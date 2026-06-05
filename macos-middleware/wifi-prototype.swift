#!/usr/bin/env swift
/**
 wifi-prototype.swift
 Sony SED-E1 WiFi Direct path exploration

 Three approaches to get WiFi data path from macOS:
   Path A: macOS creates hotspot, sends WifiConnectReq with hotspot credentials
   Path B: Glasses create P2P group, macOS joins as client
   Path C: macOS Internet Sharing as pseudo-P2P GO

 Build:
   swiftc wifi-prototype.swift -framework Foundation -framework CoreWLAN \
          -framework IOBluetooth -o wifi-prototype
*/

import Foundation
import CoreWLAN

// ── Protocol constants ───────────────────────────────────────────────────

let CMD_WIFI_STATUS_REQ:     UInt8 = 0x90  // Ask glasses WiFi state
let CMD_WIFI_STATUS_RES:     UInt8 = 0x91  // Response: 0=disabling,1=disabled,2=enabling,3=enabled
let CMD_WIFI_TURN_ON_REQ:    UInt8 = 0x92  // Tell glasses to enable WiFi
let CMD_WIFI_TURN_OFF_REQ:   UInt8 = 0x93  // Tell glasses to disable WiFi
let CMD_WIFI_CONNECT_REQ:    UInt8 = 0x94  // Send WiFi credentials (184 bytes)
let CMD_WIFI_CONN_STATUS:    UInt8 = 0x95  // Connection status: 0=disconnecting,1=disconnected,2=connecting,3=connected
let CMD_WIFI_DP_SWITCH_REQ:  UInt8 = 0x96  // Switch data path: 0=BT, 1=WiFi
let CMD_WIFI_DP_SWITCH_RES:  UInt8 = 0x97  // Switch response

// ── WifiConnectReq payload builder (0x94, 184 bytes) ─────────────────────

func buildWifiConnectReq(
    ssid: String,
    passphrase: String,
    psk: String,
    goAddr: Data,      // 4 bytes IPv4
    staAddr: Data,     // 4 bytes IPv4
    subnetMask: Data,  // 4 bytes
    dnsServer: Data,   // 4 bytes
    gateway: Data,     // 4 bytes
    goChannel: UInt16,
    acceptPort: UInt16
) -> [UInt8] {
    // Total payload: 0xB8 = 184 bytes
    var payload = [UInt8](repeating: 0, count: 184)

    // SSID at offset 0, 32 bytes max
    let ssidBytes = Array(ssid.utf8)
    for i in 0..<min(ssidBytes.count, 32) { payload[i] = ssidBytes[i] }

    // Passphrase at offset 0x20 (32), 32 bytes max
    let passBytes = Array(passphrase.utf8)
    for i in 0..<min(passBytes.count, 32) { payload[0x20 + i] = passBytes[i] }

    // Gap at offset 0x40 (64) - 32 bytes zeros (already zero)

    // Network addresses at offset 0x60 (96)
    for i in 0..<4 { payload[0x60 + i] = goAddr[i] }      // GO address
    for i in 0..<4 { payload[0x64 + i] = staAddr[i] }      // STA address
    for i in 0..<4 { payload[0x68 + i] = subnetMask[i] }   // Subnet
    for i in 0..<4 { payload[0x6C + i] = dnsServer[i] }    // DNS
    for i in 0..<4 { payload[0x70 + i] = gateway[i] }      // Gateway

    // Channel at offset 0x74, big-endian short
    payload[0x74] = UInt8(goChannel >> 8)
    payload[0x75] = UInt8(goChannel & 0xFF)

    // Accept port at offset 0x76, big-endian short
    payload[0x76] = UInt8(acceptPort >> 8)
    payload[0x77] = UInt8(acceptPort & 0xFF)

    // PSK at offset 0x78
    let pskBytes = Array(psk.utf8)
    for i in 0..<min(pskBytes.count, 184 - 0x78) { payload[0x78 + i] = pskBytes[i] }

    // Build command: [0x94] [len:2B BE] [payload]
    var cmd: [UInt8] = [CMD_WIFI_CONNECT_REQ]
    cmd.append(UInt8((payload.count >> 8) & 0xFF))
    cmd.append(UInt8(payload.count & 0xFF))
    cmd.append(contentsOf: payload)
    return cmd
}

// ── PSK derivation (PBKDF2-HMAC-SHA1, WPA2 standard) ────────────────────

func generatePSK(ssid: String, passphrase: String) -> String {
    // WPA2-PSK uses PBKDF2-HMAC-SHA1 with 4096 iterations, 256-bit key
    // This is the standard WPA2 key derivation
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    proc.arguments = ["-c", """
        import hashlib
        ssid = '\(ssid)'
        passphrase = '\(passphrase)'
        key = hashlib.pbkdf2_hmac('sha1', passphrase.encode(), ssid.encode(), 4096, 32)
        print(key.hex())
        """]
    let pipe = Pipe()
    proc.standardOutput = pipe
    try? proc.run()
    proc.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

// ── Path A: macOS creates hotspot ────────────────────────────────────────
//
// 1. Create WiFi hotspot via Internet Sharing (or programmatically)
// 2. Get hotspot SSID, passphrase, IP address, channel
// 3. Open TCP ServerSocket on a port
// 4. Send WifiConnectReq (0x94) over BT with hotspot credentials
// 5. Glasses connect to hotspot WiFi
// 6. Glasses connect TCP to our ServerSocket
// 7. Send WifiDPSwitchPathReq (0x96, mode=1) over BT
// 8. Same protocol now over TCP
//
// Pros: Simple, we control the network
// Cons: Internet Sharing UI is manual, needs scripting
//       Glasses expect WiFi Direct (P2P) SSID format
//       May not work if glasses reject non-P2P networks

func pathA_hotspot_info() {
    print("""
    ═══ PATH A: macOS Hotspot ═══

    How it works:
    1. Enable Internet Sharing (System Settings → General → Sharing → Internet Sharing)
       - Share: Thunderbolt Bridge (or Ethernet)
       - To: Wi-Fi
       - WiFi Options: set SSID and password
    2. This tool sends WifiConnectReq(0x94) over BT with the hotspot credentials
    3. Glasses join the hotspot as a WiFi client
    4. TCP connection established for high-speed data path

    Feasibility: ⚠️ MEDIUM
    - Internet Sharing creates a standard AP, not WiFi Direct
    - Glasses may reject non-DIRECT-* SSID (needs testing)
    - Manual setup required (can be scripted via defaults/networksetup)
    - If glasses accept any WPA2 network, this is the EASIEST path

    To test: Enable Internet Sharing, note SSID/password, run this tool.
    """)
}

// ── Path B: Glasses as Group Owner ───────────────────────────────────────
//
// In the original flow, the PHONE creates the P2P group.
// But what if we send WifiStatusTurnOnReq (0x92) to make the glasses
// enable their WiFi, then scan for their DIRECT-* SSID from macOS?
//
// 1. Send WifiStatusTurnOnReq (0x92) over BT
// 2. Wait for WifiStatusRes (0x91) = ENABLED
// 3. Scan WiFi for DIRECT-* networks
// 4. Connect macOS to glasses' P2P network using passphrase
// 5. Open TCP connection to glasses' listen port
//
// Problem: We don't know if glasses create their own P2P group
// The original flow has the PHONE as GO, not the glasses.
// But the glasses might have a P2P group mode we haven't found.

func pathB_glasses_GO_info() {
    print("""
    ═══ PATH B: Glasses as WiFi Direct Group Owner ═══

    How it works:
    1. Send WifiStatusTurnOnReq (0x92) over BT
    2. Glasses enable WiFi, possibly create P2P group
    3. macOS scans for DIRECT-* SSID
    4. macOS joins glasses' P2P network
    5. TCP connection to glasses' listen port

    Feasibility: ❓ UNKNOWN
    - Original flow has PHONE as GO, not glasses
    - Glasses might not create their own P2P group
    - If they do, we need to discover their passphrase
    - Easy to test: just send 0x92 and scan

    Test: Send WifiStatusTurnOnReq, wait 5s, scan for DIRECT-* networks.
    """)
}

// ── Path C: macOS WiFi Direct via CoreWLAN ───────────────────────────────
//
// macOS doesn't have standard WiFi Direct (WFD/P2P).
// But it has AWDL (Apple Wireless Direct Link) which is different.
// CoreWLAN can join WiFi Direct networks as a regular WPA2 client though.
//
// 1. Phone (Android) creates P2P group — but we don't have a phone
// 2. Alternative: macOS creates a "fake" P2P by:
//    a. Creating a network with DIRECT-XX prefix
//    b. Using standard WPA2-PSK
//    c. Glasses connect to it thinking it's P2P

func pathC_fake_P2P_info() {
    print("""
    ═══ PATH C: Fake WiFi Direct from macOS ═══

    How it works:
    1. macOS creates hotspot with SSID "DIRECT-ma-SmartEyeglass"
    2. Set WPA2-PSK passphrase
    3. Derive PSK via PBKDF2-HMAC-SHA1 (same as real WPA2)
    4. Send WifiConnectReq (0x94) with fake DIRECT-* credentials
    5. Glasses connect thinking it's a P2P group

    Feasibility: ✅ HIGH (if glasses don't validate P2P group format)
    - Internet Sharing can use any SSID including DIRECT-* prefix
    - PSK derivation is standard WPA2 (we have the code)
    - Same TCP protocol after connection
    - Only question: do glasses validate the P2P group or just connect?

    This is likely the BEST approach for macOS.
    """)
}

// ── Path analysis ────────────────────────────────────────────────────────

func printSummary() {
    print("""

    ╔══════════════════════════════════════════════════════════════════════╗
    ║  WiFi Direct Path Comparison for macOS → SED-E1                    ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║                                                                      ║
    ║  Path A: macOS Hotspot (Internet Sharing)                           ║
    ║  ├─ Effort: LOW (just enable sharing + send credentials)            ║
    ║  ├─ Risk: Glasses may reject non-DIRECT-* SSID                     ║
    ║  └─ Speed: Full WiFi throughput (~20 Mbps)                          ║
    ║                                                                      ║
    ║  Path B: Glasses as Group Owner                                     ║
    ║  ├─ Effort: LOW (send 0x92, scan, connect)                          ║
    ║  ├─ Risk: Glasses may not create P2P group without phone            ║
    ║  └─ Speed: Full WiFi throughput                                     ║
    ║                                                                      ║
    ║  Path C: Fake WiFi Direct (DIRECT-* SSID via Internet Sharing)     ║
    ║  ├─ Effort: MEDIUM (configure hotspot + PSK derivation)             ║
    ║  ├─ Risk: LOW (standard WPA2, just spoofed SSID)                   ║
    ║  └─ Speed: Full WiFi throughput                                     ║
    ║                                                                      ║
    ║  RECOMMENDED: Try Path B first (cheapest test), then Path C         ║
    ║                                                                      ║
    ║  WiFi protocol sequence (all paths):                                ║
    ║  1. BT handshake (already working)                                  ║
    ║  2. TX 0x92 WifiStatusTurnOnReq   (or skip if Path A/C)            ║
    ║  3. RX 0x91 WifiStatusRes (ENABLED=3)                              ║
    ║  4. TX 0x94 WifiConnectReq (184B: SSID+pass+PSK+IPs+port)         ║
    ║  5. RX 0x95 WifiConnectivityStatus (CONNECTED=3)                   ║
    ║  6. TX 0x96 WifiDPSwitchPathReq (mode=1=WIFI)                      ║
    ║  7. RX 0x97 WifiDPSwitchPathRes (mode=1=WIFI)                      ║
    ║  8. TCP socket now carries same binary protocol                     ║
    ║  9. BT stays for sensors/control, WiFi for display frames           ║
    ║                                                                      ║
    ║  Expected improvement: BT ~2.4fps → WiFi ~30+fps                   ║
    ╚══════════════════════════════════════════════════════════════════════╝

    """)

    // Quick PSK test
    let testPSK = generatePSK(ssid: "DIRECT-ma-SmartEyeglass", passphrase: "testpass123")
    print("PSK derivation test: PBKDF2('DIRECT-ma-SmartEyeglass', 'testpass123') = \(testPSK)")
    print("PSK length: \(testPSK.count / 2) bytes (should be 32)")
}

// ── Entry ─────────────────────────────────────────────────────────

pathA_hotspot_info()
pathB_glasses_GO_info()
pathC_fake_P2P_info()
printSummary()
