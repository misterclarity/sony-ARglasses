#!/usr/bin/env swift
/**
 glasses-tool.swift
 Sony SED-E1 macOS direct connection tool — BT + WiFi

 Modes:
   (default) — connect via BT, run Game of Life
   test      — connect via BT, REPL for diagnostic patterns
   scan      — discover nearby BT devices
   pair      — show pairing guide
   sdp       — query SDP service records
   probe     — try RFCOMM channels 1-10
   connect   — connect BT, REPL

 Build:
   swiftc glasses-tool.swift -framework IOBluetooth -framework Foundation \
          -o glasses-tool -O && chmod +x glasses-tool

 Usage:
   ./glasses-tool          (connect + GoL over BT, then optionally switch to WiFi)
   ./glasses-tool test     (connect + REPL test mode)
   ./glasses-tool scan
*/

import Foundation
import IOBluetooth
import Darwin
import zlib

// ── ANSI colours ─────────────────────────────────────────────────────────────
let CLR_RED    = "\u{001B}[31m"
let CLR_GRN    = "\u{001B}[32m"
let CLR_YLW    = "\u{001B}[33m"
let CLR_BLU    = "\u{001B}[34m"
let CLR_MAG    = "\u{001B}[35m"
let CLR_CYN    = "\u{001B}[36m"
let CLR_RST    = "\u{001B}[0m"

// ── Network helpers ────────────────────────────────────────────────────
func getInterfaceIP(_ iface: String) -> String? {
    // Use ipconfig — reliable on macOS, handles all edge cases
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/sbin/ipconfig")
    proc.arguments = ["getifaddr", iface]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError  = FileHandle.nullDevice
    guard (try? proc.run()) != nil else { return nil }
    proc.waitUntilExit()
    let ip = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                 .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    // Validate: must look like an IPv4 address
    let parts = ip.split(separator: ".")
    guard parts.count == 4, parts.allSatisfy({ UInt8($0) != nil }) else { return nil }
    return ip
}

func log(_ msg: String, color: String = CLR_RST) {
    let ts = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
    print("\(color)[\(ts)] \(msg)\(CLR_RST)")
    fflush(stdout)
    let level: String
    switch color {
    case CLR_RED: level = "ERROR"
    case CLR_YLW: level = "WARN"
    default:      level = "INFO"
    }
    emitEvent("LOG", ["level": level, "msg": msg])
}

// ── Hex helpers ───────────────────────────────────────────────────────────────
extension Data {
    var hexDump: String {
        let hex = map { String(format: "%02x", $0) }
        let asc = map { ($0 >= 0x20 && $0 < 0x7f) ? String(UnicodeScalar($0)) : "." }
        var out = ""
        for row in stride(from: 0, to: count, by: 16) {
            let end = Swift.min(row + 16, count)
            let h = hex[row..<end].joined(separator: " ").padding(toLength: 47, withPad: " ", startingAt: 0)
            let a = asc[row..<end].joined()
            out += String(format: "  %04x  %@ │%@│\n", row, h, a)
        }
        return out
    }
}

func hexToData(_ hex: String) -> Data? {
    let clean = hex.replacingOccurrences(of: " ", with: "")
                   .replacingOccurrences(of: "0x", with: "")
    guard clean.count % 2 == 0 else { return nil }
    var data = Data()
    var idx = clean.startIndex
    while idx < clean.endIndex {
        let next = clean.index(idx, offsetBy: 2)
        guard let byte = UInt8(clean[idx..<next], radix: 16) else { return nil }
        data.append(byte)
        idx = next
    }
    return data
}

// ── Capture log ───────────────────────────────────────────────────────────────
class CaptureLog {
    let path: String
    private var fh: FileHandle?
    private var byteCount = 0
    private let queue = DispatchQueue(label: "glasses.capture", qos: .utility)

    init(path: String) {
        self.path = path
        FileManager.default.createFile(atPath: path, contents: nil)
        fh = FileHandle(forWritingAtPath: path)
        log("📝 Capture log: \(path)", color: CLR_YLW)
    }

    func write(direction: String, data: Data) {
        let count = data.count
        let hex = data.hexDump
        let ts = Date().timeIntervalSince1970
        let tsInt = Int(ts)
        let tsFrac = Int((ts - Double(tsInt)) * 1000)
        let header = "[\(tsInt).\(String(format: "%03d", tsFrac))] \(direction) \(count) bytes\n"

        let dirColor = direction.contains("TX") ? CLR_BLU : CLR_GRN
        print("\(dirColor)\(header)\(hex)\(CLR_RST)", terminator: "")
        fflush(stdout)

        queue.async { [weak self] in
            guard let self = self else { return }
            self.byteCount += count
            let line = header + hex + "\n"
            self.fh?.write(Data(line.utf8))
        }
    }

    func close() {
        queue.sync { fh?.closeFile() }
        log("Capture saved: \(byteCount) bytes total → \(path)", color: CLR_YLW)
    }
}

// ── JSON Event Log ──────────────────────────────────────────────────────────────
class JSONEventLog {
    let path: String
    private var fh: FileHandle?
    private let queue = DispatchQueue(label: "glasses.jsonlog", qos: .utility)

    init(path: String) {
        self.path = path
        FileManager.default.createFile(atPath: path, contents: nil)
        fh = FileHandle(forWritingAtPath: path)
    }

    func write(_ fields: [String: Any]) {
        let ts = Date().timeIntervalSince1970 * 1000.0
        var obj = fields
        obj["ts"] = ts
        queue.async { [weak self] in
            guard let self = self, let fh = self.fh else { return }
            if let data = try? JSONSerialization.data(withJSONObject: obj) {
                fh.write(data)
                fh.write(Data("\n".utf8))
            }
        }
    }

    func close() {
        queue.sync { fh?.closeFile() }
    }
}

func emitEvent(_ type: String, _ extra: [String: Any] = [:]) {
    var fields = extra
    fields["type"] = type
    gJSONLog?.write(fields)
}

// ── RFCOMM delegate ───────────────────────────────────────────────────────────
class RFCOMMDelegate: NSObject, IOBluetoothRFCOMMChannelDelegate {

    var onData: ((Data) -> Void)?
    var onOpen: ((IOBluetoothRFCOMMChannel) -> Void)?
    var onClose: (() -> Void)?

    func rfcommChannelOpenComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!,
                                   status error: IOReturn) {
        if error == kIOReturnSuccess {
            log("✅ RFCOMM channel open (MTU: \(rfcommChannel.getMTU()))", color: CLR_GRN)
            gChannel = rfcommChannel
            onOpen?(rfcommChannel)
        } else {
            log("❌ RFCOMM open failed: 0x\(String(format: "%08x", error))", color: CLR_RED)
        }
    }

    func rfcommChannelClosed(_ rfcommChannel: IOBluetoothRFCOMMChannel!) {
        log("🔴 RFCOMM closed", color: CLR_RED)
        onClose?()
    }

    func rfcommChannelData(_ rfcommChannel: IOBluetoothRFCOMMChannel!,
                           data dataPointer: UnsafeMutableRawPointer!,
                           length dataLength: Int) {
        let data = Data(bytes: dataPointer, count: dataLength)
        onData?(data)
    }

    func rfcommChannelWriteComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!,
                                    refcon: UnsafeMutableRawPointer!,
                                    status error: IOReturn) {
        if error != kIOReturnSuccess {
            log("Write error: 0x\(String(format: "%08x", error))", color: CLR_RED)
        }
    }
}

// ── Commands ──────────────────────────────────────────────────────────────────

// MARK: scan
func cmdScan(duration: TimeInterval = 10.0) {
    log("🔍 Scanning for Bluetooth devices (\(Int(duration))s)...", color: CLR_CYN)
    log("   Power on the glasses, hold POWER switch 4+ seconds until text appears.", color: CLR_YLW)

    var seen = Set<String>()

    if let devices = IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice] {
        log("Paired devices:", color: CLR_MAG)
        for d in devices {
            let name = d.name ?? "(unknown)"
            let addr = d.addressString ?? ""
            log("  📱 \(name)  [\(addr)]", color: CLR_MAG)
            seen.insert(addr)
        }
    }

    guard let inquiry = IOBluetoothDeviceInquiry(delegate: nil) else {
        log("Failed to create inquiry", color: CLR_RED); return
    }
    inquiry.inquiryLength = UInt8(Swift.min(duration, 30))
    inquiry.start()

    let deadline = Date().addingTimeInterval(duration)
    while Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        if let found = inquiry.foundDevices() as? [IOBluetoothDevice] {
            for d in found {
                let addr = d.addressString ?? ""
                guard !seen.contains(addr) else { continue }
                seen.insert(addr)
                let name = d.name ?? "(discovering...)"
                let cls  = d.deviceClassMajor
                let icon = cls == kBluetoothDeviceClassMajorAudio ? "🎧" :
                           cls == kBluetoothDeviceClassMajorPhone  ? "📱" :
                           cls == kBluetoothDeviceClassMajorComputer ? "💻" : "📡"
                log("\(icon) Found: \(name)  [\(addr)]", color: CLR_GRN)
            }
        }
    }
    inquiry.stop()
    log("Scan complete. \(seen.count) devices total.", color: CLR_CYN)
}

// MARK: sdp
func cmdSDP(address: String) {
    guard let device = IOBluetoothDevice(addressString: address) else {
        log("Device not found: \(address)", color: CLR_RED); return
    }
    log("Querying SDP records for \(device.name ?? address)...", color: CLR_CYN)
    device.performSDPQuery(nil)
    RunLoop.current.run(until: Date().addingTimeInterval(5))

    guard let services = device.services as? [IOBluetoothSDPServiceRecord] else {
        log("No SDP records found", color: CLR_RED); return
    }
    log("SDP services (\(services.count)):", color: CLR_GRN)
    for (i, svc) in services.enumerated() {
        var channelID: BluetoothRFCOMMChannelID = 0
        let hasRFCOMM = svc.getRFCOMMChannelID(&channelID) == kIOReturnSuccess
        let nameAttr = svc.getAttributeDataElement(
            BluetoothSDPServiceAttributeID(kBluetoothSDPAttributeIdentifierServiceName.rawValue))
        let name = nameAttr?.getStringValue() ?? "(unnamed)"
        if hasRFCOMM {
            log("  [\(i)] \(CLR_BLU)\(name)\(CLR_GRN) — RFCOMM channel \(channelID)", color: CLR_GRN)
        } else {
            log("  [\(i)] \(name)", color: CLR_RST)
        }
    }
}

// ── Global references ─────────────────────────────────────────────────────────
var gChannel: IOBluetoothRFCOMMChannel?
var gJSONLog: JSONEventLog?
var gDelegate: RFCOMMDelegate?
var gCapture: CaptureLog?
let gWriteQueue = DispatchQueue(label: "glasses.rfcomm.write", qos: .userInitiated)

enum DisplayMode { case test, gol }
var gDisplayMode: DisplayMode = .gol

// MARK: connect
func cmdConnect(address: String, channel: BluetoothRFCOMMChannelID = 0,
                captureFile: String? = nil) {
    guard let device = IOBluetoothDevice(addressString: address) else {
        log("Cannot create device for \(address) — is it paired?", color: CLR_RED)
        return
    }

    let name = device.name ?? address
    log("Connecting to \(name) [\(address)]...", color: CLR_CYN)
    saveLastUsed(address)   // remember for next run (skips scan)
    gJSONLog = JSONEventLog(path: "/tmp/glasses-events.jsonl")

    device.performSDPQuery(nil)
    RunLoop.current.run(until: Date().addingTimeInterval(4))

    var rfcommChannel: BluetoothRFCOMMChannelID = channel
    if rfcommChannel == 0 {
        if let services = device.services as? [IOBluetoothSDPServiceRecord] {
            for svc in services {
                var ch: BluetoothRFCOMMChannelID = 0
                if svc.getRFCOMMChannelID(&ch) == kIOReturnSuccess {
                    log("SPP found on channel \(ch) via SDP", color: CLR_GRN)
                    rfcommChannel = ch; break
                }
            }
        }
        if rfcommChannel == 0 { rfcommChannel = 1 }
    }

    gCapture = captureFile.map { CaptureLog(path: $0) }
    gDelegate = RFCOMMDelegate()

    var rxCount = 0
    // BT handshake phases:
    // 0=wait ProtocolVersion  1=wait SettingsStatusResponse
    // 2=wait VersionResponse  3=sent NewHostApp, wait FotaStatus
    // 4=sent SyncResponse, wait LevelNotification/OpenAppStartResponse
    // 5=display ready, sending frames
    var initPhase = 0

    // ── WiFi state ─────────────────────────────────────────────────────────────
    // wifiPhase: 0=off  10=WifiTurnOnReq sent (wait 0x91 ENABLED)
    //            11=WifiConnectReq sent (wait 0x95 CONNECTED)
    //            12=TCP accepted (wait for user to 'wifi switch')
    //            13=WiFi data path ACTIVE
    var wifiPhase  = 0
    var wifiActive = false     // true when display frames go over TCP
    var wifiPort   = 0         // OS-assigned TCP port for ServerSocket
    var wifiServerFd: Int32 = -1
    var wifiClientFd: Int32 = -1
    var wifiSSID   = ""   // populated from .env SSID=
    // Auto-upgrade BT→WiFi after phase5 when .env credentials are present
    let autoWifi   = !config.wifiSSID.isEmpty && (getInterfaceIP("en0") != nil)

    // ── emitState helper ──────────────────────────────────────────────────────
    func emitState() {
        emitEvent("STATE", [
            "phase": initPhase, "wifi_phase": wifiPhase, "wifi_active": wifiActive,
            "tcp_connected": wifiClientFd >= 0, "bt_connected": gChannel != nil
        ])
    }

    // ── RFCOMM reassembly buffer ──────────────────────────────────────────────
    // Multiple wire frames can arrive in one RFCOMM chunk. We buffer incomplete
    // data here and parse complete frames [cmdId:1B][len:2B][payload:lenB] one
    // at a time, so every frame gets its own pass through the dispatch switch.
    var rxBuf = Data()
    var wifiPass   = ""   // populated from .env PSWD=
    var wifiGoIP   = ""   // macOS WiFi IP (en0); populated at connect time
    let wifiWriteQueue = DispatchQueue(label: "glasses.wifi.write", qos: .userInitiated)

    // ── Send over BT RFCOMM (chunked to MTU) ──────────────────────────────────
    func sendCmd(_ bytes: [UInt8], label: String) {
        guard let ch = gChannel else { log("TX failed: no channel", color: CLR_RED); return }
        let mtu = Int(ch.getMTU())
        let chunkSize = mtu > 0 ? mtu : 665
        gWriteQueue.sync {
            var offset = 0; var ok = true
            while offset < bytes.count {
                let end = min(offset + chunkSize, bytes.count)
                var chunk = Array(bytes[offset..<end])
                let r = ch.writeSync(&chunk, length: UInt16(chunk.count))
                if r != 0 {
                    log("TX chunk error at \(offset): 0x\(String(format:"%x",r))", color: CLR_RED)
                    ok = false; break
                }
                offset = end
            }
            let preview = bytes.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" ")
            log("→ BT TX \(label) \(bytes.count)B \(ok ? "OK" : "FAIL"): \(preview)...", color: CLR_BLU)
            let cmdHex = bytes.isEmpty ? "0x00" : String(format: "0x%02x", bytes[0])
            emitEvent("TX", ["cmd": cmdHex, "name": label, "bytes": bytes.count,
                             "phase": initPhase, "wifi_active": wifiActive, "ok": ok])
        }
        gCapture?.write(direction: " TX(BT)", data: Data(bytes))
    }

    // ── Send over WiFi TCP ─────────────────────────────────────────────────────
    func sendViaTCP(_ bytes: [UInt8], label: String) {
        guard wifiClientFd >= 0 else {
            log("⚠️ WiFi fd not ready, falling back to BT: \(label)", color: CLR_YLW)
            sendCmd(bytes, label: label + "(BT-fallback)")
            return
        }
        wifiWriteQueue.sync {
            var buf = bytes
            let written = Darwin.write(wifiClientFd, &buf, buf.count)
            let preview = bytes.prefix(12).map{String(format:"%02x",$0)}.joined(separator:" ")
            if written == bytes.count {
                log("→ WiFi TX \(label) \(bytes.count)B OK: \(preview)...", color: CLR_BLU)
            } else {
                log("⚠️ WiFi TX partial \(written)/\(bytes.count): \(label)", color: CLR_RED)
            }
            let cmdHexW = bytes.isEmpty ? "0x00" : String(format: "0x%02x", bytes[0])
            emitEvent("TX", ["cmd": cmdHexW, "name": label, "bytes": bytes.count,
                             "phase": initPhase, "wifi_active": true, "ok": written == bytes.count])
        }
        gCapture?.write(direction: " TX(WiFi)", data: Data(bytes))
    }

    // ── Create TCP ServerSocket (OS picks port) ────────────────────────────────
    func wifiCreateServer() -> Int {
        if wifiServerFd >= 0 { Darwin.close(wifiServerFd); wifiServerFd = -1 }

        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else {
            log("❌ socket() failed: \(String(cString: strerror(errno)))", color: CLR_RED)
            return -1
        }
        var one: Int32 = 1
        Darwin.setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len    = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = 0   // INADDR_ANY
        addr.sin_port   = 0        // OS assigns

        let bindRet = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindRet == 0 else {
            log("❌ bind() failed: \(String(cString: strerror(errno)))", color: CLR_RED)
            Darwin.close(fd); return -1
        }
        guard Darwin.listen(fd, 1) == 0 else {
            log("❌ listen() failed: \(String(cString: strerror(errno)))", color: CLR_RED)
            Darwin.close(fd); return -1
        }

        // Read back OS-assigned port
        var addrLen = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.getsockname(fd, $0, &addrLen)
            }
        }
        let port = Int(UInt16(bigEndian: addr.sin_port))

        wifiServerFd = fd
        wifiPort = port
        log("🌐 WiFi TCP server listening on port \(port)", color: CLR_GRN)
        return port
    }

    // ── Accept one TCP connection in background ────────────────────────────────
    func wifiStartAccept() {
        let serverFd = wifiServerFd
        DispatchQueue.global(qos: .userInitiated).async {
            guard serverFd >= 0 else { return }
            log("⏳ Waiting for glasses TCP connection on port \(wifiPort)...", color: CLR_YLW)
            let clientFd = Darwin.accept(serverFd, nil, nil)
            guard clientFd >= 0 else {
                if errno != EBADF {  // EBADF = server socket closed intentionally
                    log("❌ accept() failed: \(String(cString: strerror(errno)))", color: CLR_RED)
                }
                return
            }
            wifiClientFd = clientFd
            DispatchQueue.main.async {
                log("✅ Glasses TCP connected! fd=\(clientFd)", color: CLR_GRN)
                log("   Type 'wifi switch' to activate WiFi data path.", color: CLR_YLW)
                wifiPhase = 12
                emitState()
                if autoWifi {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        log("🌐 Auto: switching to WiFi data path...", color: CLR_CYN)
                        sendCmd([0x96, 0x00, 0x01, 0x01], label: "WifiDPSwitchPathReq(WIFI)")
                    }
                }
            }

            // Read loop — feed to same protocol handler as BT
            var buf = [UInt8](repeating: 0, count: 8192)
            while true {
                let n = Darwin.read(clientFd, &buf, buf.count)
                if n <= 0 { break }
                let data = Data(buf[0..<n])
                gCapture?.write(direction: " RX(WiFi)", data: data)
                DispatchQueue.main.async {
                    log("← WiFi RX \(n)B:", color: CLR_GRN)
                    print(data.hexDump)
                    gDelegate?.onData?(data)
                }
            }
            log("🔴 WiFi TCP connection closed (n<=0)", color: CLR_RED)
            wifiClientFd = -1
            DispatchQueue.main.async {
                if wifiActive {
                    wifiActive = false; wifiPhase = 0
                    emitEvent("WIFI", ["event": "DROPPED", "state": 0])
                    emitState()
                    log("⬅️ WiFi dropped — BT is now active for all commands.", color: CLR_YLW)
                }
            }
        }
    }

    // ── PSK derivation: PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32) ─────────
    func derivePSK(ssid: String, passphrase: String) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        // Escape single-quotes in ssid/passphrase by using a heredoc approach
        let script = """
import hashlib,sys
args=sys.argv[1:]
psk=hashlib.pbkdf2_hmac('sha1',args[1].encode('utf-8'),args[0].encode('utf-8'),4096,32)
print(psk.hex())
"""
        proc.arguments = ["-c", script, ssid, passphrase]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            log("⚠️ PSK derivation failed: \(error)", color: CLR_RED)
            return ""
        }
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                      .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return out
    }

    // ── Build WifiConnectReq (0x94) — 184-byte payload ────────────────────────
    // Payload layout (smali-verified):
    //   0x00-0x1F: SSID (32B, UTF-8, null-padded)
    //   0x20-0x3F: passphrase (32B, UTF-8, null-padded)
    //   0x40-0x5F: reserved zeros (32B)
    //   0x60-0x63: goAddr (4B, IPv4, big-endian)
    //   0x64-0x67: staAddr (4B, IPv4 — 0.0.0.0 on same-network WiFi, glasses use DHCP)
    //   0x68-0x6B: subnetMask (4B, big-endian)
    //   0x6C-0x6F: dnsServer (4B, zeros)
    //   0x70-0x73: gateway (4B, zeros)
    //   0x74-0x75: goChannel (2B, MHz, big-endian short)
    //   0x76-0x77: acceptPortNum (2B, big-endian short) ← TCP server port
    //   0x78-0xB7: PSK hex string (64 chars, UTF-8, null-padded)
    func buildWifiConnectReq(ssid: String, passphrase: String, psk: String,
                              goIP: String, port: Int, channelMHz: Int) -> [UInt8] {
        var payload = [UInt8](repeating: 0, count: 184)

        // SSID at 0x00
        let ssidB = Array(ssid.utf8.prefix(32))
        for i in 0..<ssidB.count { payload[i] = ssidB[i] }

        // Passphrase at 0x20
        let passB = Array(passphrase.utf8.prefix(32))
        for i in 0..<passB.count { payload[0x20 + i] = passB[i] }

        // Parse goIP
        let octets = goIP.split(separator: ".").compactMap { UInt8($0) }
        guard octets.count == 4 else {
            log("❌ Invalid IP: \(goIP). Need 4 octets.", color: CLR_RED)
            return []
        }

        // goAddr at 0x60
        for i in 0..<4 { payload[0x60 + i] = octets[i] }

        // staAddr at 0x64 — set to 0.0.0.0 on same-network WiFi; glasses use router DHCP
        // (WiFi Direct P2P: was flip last octet to assign glasses a static IP from Mac's DHCP)
        for i in 0..<4 { payload[0x64 + i] = 0 }

        // Subnet 255.255.255.0 at 0x68
        payload[0x68] = 255; payload[0x69] = 255; payload[0x6A] = 255; payload[0x6B] = 0

        // DNS = 0, gateway = 0 (already zeroed)

        // goChannel at 0x74 (big-endian short)
        payload[0x74] = UInt8((channelMHz >> 8) & 0xFF)
        payload[0x75] = UInt8(channelMHz & 0xFF)

        // acceptPortNum at 0x76 (big-endian short)
        payload[0x76] = UInt8((port >> 8) & 0xFF)
        payload[0x77] = UInt8(port & 0xFF)

        // PSK hex string at 0x78 (64 chars)
        let pskB = Array(psk.utf8.prefix(64))
        for i in 0..<pskB.count { payload[0x78 + i] = pskB[i] }

        // Full command: [0x94][0x00][0xB8][payload]
        var cmd: [UInt8] = [0x94, 0x00, 0xB8]
        cmd += payload
        return cmd
    }

    // ── Detect WiFi channel from macOS ────────────────────────────────────────
    // airport -I is muted by macOS privacy in Ventura+; use system_profiler fallback.
    // Returns the 2.4GHz MHz equivalent — glasses firmware is 2.4GHz only.
    func detectWifiChannel() -> Int {
        // Try airport first (works on older macOS)
        let airportProc = Process()
        airportProc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        airportProc.arguments = ["bash", "-c",
            "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport -I 2>/dev/null | awk '/channel:/{print $2}' | head -1"]
        let airportPipe = Pipe()
        airportProc.standardOutput = airportPipe
        airportProc.standardError  = FileHandle.nullDevice
        try? airportProc.run(); airportProc.waitUntilExit()
        let raw = String(data: airportPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                      .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let chStr = raw.split(separator: ",").first.flatMap(String.init) ?? raw
        if let ch = Int(chStr), ch >= 1, ch <= 14 {
            let mhz = 2407 + ch * 5
            log("📻 Detected channel \(ch) = \(mhz) MHz (airport)", color: CLR_CYN)
            return mhz
        }

        // Fallback: system_profiler (Ventura+) — parse first 2.4GHz channel found.
        // The glasses are 2.4GHz-only hardware; even if Mac is on 5GHz the AP
        // usually has a 2.4GHz band on the same SSID.
        let spProc = Process()
        spProc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        spProc.arguments = ["bash", "-c",
            "system_profiler SPAirPortDataType 2>/dev/null | grep 'Channel:.*2GHz' | head -1 | sed 's/.*Channel: \\([0-9]*\\).*/\\1/'"]
        let spPipe = Pipe()
        spProc.standardOutput = spPipe
        spProc.standardError  = FileHandle.nullDevice
        try? spProc.run(); spProc.waitUntilExit()
        let spOut = String(data: spPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if let ch = Int(spOut), ch >= 1, ch <= 14 {
            let mhz = 2407 + ch * 5
            log("📻 Detected 2.4GHz channel \(ch) = \(mhz) MHz (system_profiler)", color: CLR_CYN)
            return mhz
        }

        log("📻 Defaulting to 2.4GHz ch6 (2437 MHz) — airport/system_profiler unavailable", color: CLR_YLW)
        return 2437
    }

    // ── Detect macOS WiFi IP (en0 — same network the glasses will join) ─────────
    func detectWifiIP() {
        let ip = getInterfaceIP("en0") ?? ""
        if !ip.isEmpty {
            wifiGoIP = ip
            log("✅ WiFi IP (en0): \(ip)  (saved to wifiGoIP)", color: CLR_GRN)
        } else {
            log("⚠️ Could not detect en0 IP. Are you connected to WiFi?", color: CLR_YLW)
            log("   Run: ipconfig getifaddr en0", color: CLR_YLW)
        }
    }

    // ── Start WiFi connect sequence ────────────────────────────────────────────
    func wifiStartConnect(ssid: String, pass: String, goIP: String) {
        let resolvedIP = goIP.isEmpty ? (getInterfaceIP("en0") ?? "") : goIP
        guard !resolvedIP.isEmpty else {
            log("❌ Cannot determine Mac WiFi IP. Run: ipconfig getifaddr en0", color: CLR_RED)
            return
        }
        if wifiGoIP.isEmpty { wifiGoIP = resolvedIP }
        // 1. Create TCP ServerSocket first (glasses must find port to connect to)
        let port = wifiCreateServer()
        guard port > 0 else { return }

        // 2. Start background accept thread
        wifiStartAccept()

        // 3. Detect WiFi channel
        let channelMHz = detectWifiChannel()

        // 4. Derive PSK
        log("🔑 Deriving PSK for '\(ssid)'...", color: CLR_CYN)
        let psk = derivePSK(ssid: ssid, passphrase: pass)
        guard !psk.isEmpty else {
            log("❌ PSK derivation failed", color: CLR_RED); return
        }
        log("   PSK: \(psk.prefix(16))... (\(psk.count) chars)", color: CLR_CYN)

        // 5. Build and send WifiConnectReq over BT
        let req = buildWifiConnectReq(ssid: ssid, passphrase: pass, psk: psk,
                                       goIP: resolvedIP, port: port, channelMHz: channelMHz)
        guard !req.isEmpty else { return }
        sendCmd(req, label: "WifiConnectReq(0x94)")
        wifiPhase = 11
        emitState()

        log("", color: CLR_RST)
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", color: CLR_CYN)
        log("📡 WifiConnectReq sent. Waiting for glasses to join WiFi...", color: CLR_CYN)
        log("   SSID:       \(ssid)", color: CLR_YLW)
        log("   Passphrase: \(pass)", color: CLR_YLW)
        log("   macOS IP:   \(goIP)", color: CLR_YLW)
        log("   TCP port:   \(port)  ← glasses will connect here", color: CLR_YLW)
        log("   Channel:    \(channelMHz) MHz", color: CLR_YLW)
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", color: CLR_CYN)
        log("Watch for: 0x95 CONNECTING → 0x95 CONNECTED → TCP accept", color: CLR_YLW)
        log("Then type: wifi switch", color: CLR_YLW)
    }

    // ── WiFi REPL help ─────────────────────────────────────────────────────────
    func printWifiHelp() {
        print("""
        \(CLR_CYN)
        ═══ WiFi COMMANDS ═══
        macOS = TCP server. Glasses TCP-connect back to us.
        Sequence: wifi on → wifi connect → wifi switch\(CLR_RST)

        \(CLR_GRN)wifi on\(CLR_RST)                         enable glasses WiFi radio (0x92)
        \(CLR_GRN)wifi status\(CLR_RST)                     query glasses WiFi state (0x90)
        \(CLR_GRN)wifi connect [ssid] [pass] [ip]\(CLR_RST) start TCP server + send WifiConnectReq (0x94)
        \(CLR_GRN)wifi switch\(CLR_RST)                     switch display path to WiFi (0x96 mode=1)
        \(CLR_GRN)wifi bt\(CLR_RST)                         switch back to BT (0x96 mode=0)
        \(CLR_GRN)wifi off\(CLR_RST)                        disable glasses WiFi (0x93)
        \(CLR_GRN)wifi ip\(CLR_RST)                         detect macOS WiFi IP (en0)

        \(CLR_YLW)SETUP (before 'wifi connect'):\(CLR_RST)
          1. Connect THIS Mac to the same WiFi network the glasses will join
          2. Put credentials in macos-middleware/.env:
               SSID=YourNetwork
               PSWD=YourPassword
          3. Verify Mac is on WiFi: ipconfig getifaddr en0

        \(CLR_CYN)Current config:\(CLR_RST)
          SSID:     \(wifiSSID)
          Password: \(wifiPass)
          macOS IP: \(wifiGoIP)
          TCP port: \(wifiPort > 0 ? String(wifiPort) : "(not started)")
          wifiPhase: \(wifiPhase)   wifiActive: \(wifiActive)
        """)
        print("\(CLR_MAG)> \(CLR_RST)", terminator: "")
        fflush(stdout)
    }

    // ── Auto WiFi upgrade
    func triggerAutoWifi() {
        guard autoWifi else { return }
        log("🌐 Auto WiFi upgrade (SSID: \(config.wifiSSID))...", color: CLR_CYN)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            sendCmd([0x92, 0x00, 0x00], label: "WifiStatusTurnOnReq")
            wifiPhase = 10
            emitState()
        }
    }

    // ── Ready banner ─────────────────────────────────────────────────────
    func printReadyBanner() {
        print("""
        \(CLR_GRN)
        ════════════════════════════════════════════════
        🕶  CONNECTED — glasses ready
        ════════════════════════════════════════════════\(CLR_RST)
        \(CLR_YLW)glider\(CLR_RST)        start glider demo (~2.5fps BT)
        \(CLR_YLW)stop\(CLR_RST)          stop demo
        \(CLR_YLW)wifi setup\(CLR_RST)    step-by-step WiFi instructions (30fps)
        \(CLR_YLW)wifi on\(CLR_RST)       enable glasses WiFi radio
        \(CLR_YLW)wifi connect auto\(CLR_RST)   connect using .env credentials
        \(CLR_YLW)wifi switch\(CLR_RST)   activate WiFi path (30fps, auto-starts glider)
        \(CLR_YLW)help\(CLR_RST)          all commands
        """)
        print("\(CLR_MAG)> \(CLR_RST)", terminator: "")
        fflush(stdout)
    }

    // ── WiFi setup guide ─────────────────────────────────────────────────────
    func printWifiSetup() {
        let ssid = config.wifiSSID.isEmpty ? wifiSSID : config.wifiSSID
        let pswd = config.wifiPSWD.isEmpty ? wifiPass : config.wifiPSWD
        let ip   = getInterfaceIP("en0") ?? "10.x.x.x"
        let cmd  = "wifi connect \(ssid.isEmpty ? "<ssid>" : ssid) \(pswd.isEmpty ? "<pass>" : pswd) \(ip)"
        print("""
        \(CLR_CYN)\n═══ WiFi SETUP ═══\(CLR_RST)
        1. \(CLR_GRN)wifi on\(CLR_RST)       — enable glasses WiFi radio
           Wait: \"WifiStatusRes(0x91): ENABLED\"

        2. \(CLR_YLW)\(cmd)\(CLR_RST)
           (or: wifi connect auto)
           Wait: \"WifiConnectivityStatus(0x95): CONNECTED\"
                 \"Glasses TCP connected!\"

        3. \(CLR_GRN)wifi switch\(CLR_RST)    — move display path to WiFi
           On 0x97 confirmed: 30fps glider starts automatically

        4. \(CLR_GRN)stop\(CLR_RST) / \(CLR_GRN)glider\(CLR_RST) / \(CLR_GRN)wifi bt\(CLR_RST) — pause / restart / back to BT
        ════════════════════
        """)
        print("\(CLR_MAG)> \(CLR_RST)", terminator: "")
        fflush(stdout)
    }

    // ── Game of Life engine ───────────────────────────────────────────────────
    let W = 419
    let H = 138
    var grid: [[Bool]] = []
    var golGeneration = 0
    var golTimer: Timer?


    func golInit() {
        grid = Array(repeating: Array(repeating: false, count: W), count: H)
        golGeneration = 0
    }

    func golStep() {
        var next = Array(repeating: Array(repeating: false, count: W), count: H)
        for y in 0..<H { for x in 0..<W {
            var n = 0
            for dy in -1...1 { for dx in -1...1 {
                if dx == 0 && dy == 0 { continue }
                if grid[(y+dy+H)%H][(x+dx+W)%W] { n += 1 }
            }}
            next[y][x] = grid[y][x] ? (n == 2 || n == 3) : (n == 3)
        }}
        grid = next
        golGeneration += 1
    }

    // ── 5×7 pixel font (ASCII 32-127) — one byte per column, LSB = top row ───
    let font5x7: [UInt8] = [
        // SPC !   "   #   $   %   &   '  (   )   *   +   ,   -   .   /
        0,0,0,0,0,  0x5F,0,0,0,0,  0x03,0,0x03,0,0,  0x14,0x7F,0x14,0x7F,0x14,
        0x24,0x2A,0x7F,0x2A,0x12,  0x23,0x13,0x08,0x64,0x62,  0x36,0x49,0x55,0x22,0x50,  0x03,0,0,0,0,
        0,0x1C,0x22,0x41,0,  0,0x41,0x22,0x1C,0,  0x14,0x08,0x3E,0x08,0x14,  0x08,0x08,0x3E,0x08,0x08,
        0,0x50,0x30,0,0,  0x08,0x08,0x08,0x08,0x08,  0,0x60,0x60,0,0,  0x20,0x10,0x08,0x04,0x02,
        // 0-9
        0x3E,0x51,0x49,0x45,0x3E,  0,0x42,0x7F,0x40,0,  0x42,0x61,0x51,0x49,0x46,  0x21,0x41,0x45,0x4B,0x31,
        0x18,0x14,0x12,0x7F,0x10,  0x27,0x45,0x45,0x45,0x39,  0x3C,0x4A,0x49,0x49,0x30,  0x01,0x71,0x09,0x05,0x03,
        0x36,0x49,0x49,0x49,0x36,  0x06,0x49,0x49,0x29,0x1E,
        // :   ;   <   =   >   ?   @
        0,0x36,0x36,0,0,  0,0x56,0x36,0,0,  0x08,0x14,0x22,0x41,0,  0x14,0x14,0x14,0x14,0x14,
        0,0x41,0x22,0x14,0x08,  0x02,0x01,0x51,0x09,0x06,  0x32,0x49,0x79,0x41,0x3E,
        // A-Z
        0x7E,0x11,0x11,0x11,0x7E,  0x7F,0x49,0x49,0x49,0x36,  0x3E,0x41,0x41,0x41,0x22,  0x7F,0x41,0x41,0x22,0x1C,
        0x7F,0x49,0x49,0x49,0x41,  0x7F,0x09,0x09,0x09,0x01,  0x3E,0x41,0x49,0x49,0x7A,  0x7F,0x08,0x08,0x08,0x7F,
        0,0x41,0x7F,0x41,0,  0x20,0x40,0x41,0x3F,0x01,  0x7F,0x08,0x14,0x22,0x41,  0x7F,0x40,0x40,0x40,0x40,
        0x7F,0x02,0x0C,0x02,0x7F,  0x7F,0x04,0x08,0x10,0x7F,  0x3E,0x41,0x41,0x41,0x3E,  0x7F,0x09,0x09,0x09,0x06,
        0x3E,0x41,0x51,0x21,0x5E,  0x7F,0x09,0x19,0x29,0x46,  0x46,0x49,0x49,0x49,0x31,  0x01,0x01,0x7F,0x01,0x01,
        0x3F,0x40,0x40,0x40,0x3F,  0x1F,0x20,0x40,0x20,0x1F,  0x3F,0x40,0x38,0x40,0x3F,  0x63,0x14,0x08,0x14,0x63,
        0x07,0x08,0x70,0x08,0x07,  0x61,0x51,0x49,0x45,0x43,
        // [  \  ]  ^  _  `  a-z
        0,0x7F,0x41,0x41,0,  0x02,0x04,0x08,0x10,0x20,  0,0x41,0x41,0x7F,0,
        0x04,0x02,0x01,0x02,0x04,  0x40,0x40,0x40,0x40,0x40,  0,0x01,0x02,0x04,0,
        0x20,0x54,0x54,0x54,0x78,  0x7F,0x48,0x44,0x44,0x38,  0x38,0x44,0x44,0x44,0x20,  0x38,0x44,0x44,0x48,0x7F,
        0x38,0x54,0x54,0x54,0x18,  0x08,0x7E,0x09,0x01,0x02,  0x0C,0x52,0x52,0x52,0x3E,  0x7F,0x08,0x04,0x04,0x78,
        0,0x44,0x7D,0x40,0,  0x20,0x40,0x44,0x3D,0,  0x7F,0x10,0x28,0x44,0,  0,0x41,0x7F,0x40,0,
        0x7C,0x04,0x18,0x04,0x78,  0x7C,0x08,0x04,0x04,0x78,  0x38,0x44,0x44,0x44,0x38,  0x7C,0x14,0x14,0x14,0x08,
        0x08,0x14,0x14,0x18,0x7C,  0x7C,0x08,0x04,0x04,0x08,  0x48,0x54,0x54,0x54,0x20,  0x04,0x3F,0x44,0x40,0x20,
        0x3C,0x40,0x40,0x20,0x7C,  0x1C,0x20,0x40,0x20,0x1C,  0x3C,0x40,0x30,0x40,0x3C,  0x44,0x28,0x10,0x28,0x44,
        0x0C,0x50,0x50,0x50,0x3C,  0x44,0x64,0x54,0x4C,0x44,
        // {  |  }  ~  DEL
        0,0x08,0x36,0x41,0,  0,0,0x7F,0,0,  0,0x41,0x36,0x08,0,  0x02,0x01,0x02,0x04,0x02,  0x3E,0x49,0x49,0x49,0x3E
    ]

    func drawText(_ text: String, x: Int, y: Int, into img: inout [UInt8]) {
        var cx = x
        for ch in text.unicodeScalars {
            let code = Int(ch.value)
            guard code >= 32 && code <= 127 else { cx += 6; continue }
            let base = (code - 32) * 5
            guard base + 4 < font5x7.count else { cx += 6; continue }
            for col in 0..<5 {
                let colBits = font5x7[base + col]
                for row in 0..<7 {
                    let px = cx + col
                    let py = y + row
                    guard px >= 0 && px < W && py >= 0 && py < H else { continue }
                    if (colBits >> row) & 1 == 1 {
                        img[py * W + px] = 255
                    }
                }
            }
            cx += 6
        }
    }

    func golToImage() -> [UInt8] {
        var img = [UInt8](repeating: 0, count: W * H)

        // GoL cells — avoid border (1px) and text area (bottom 10px)
        let yLimit = H - 11   // leave room for border + text row
        for y in 1..<yLimit { for x in 1..<(W-1) {
            if grid[y][x] { img[y * W + x] = 255 }
        }}

        // Always-on 1px border showing the full rendering surface
        for x in 0..<W { img[x] = 255; img[(H-1)*W+x] = 255 }
        for y in 0..<H { img[y*W] = 255; img[y*W+W-1] = 255 }

        // Separator line above text area
        let sepY = H - 10
        for x in 1..<(W-1) { img[sepY*W+x] = 128 }

        // Label text in the bottom strip
        drawText("SED-E1  GAME OF LIFE  gen:\(golGeneration)", x: 8, y: H-9, into: &img)

        return img
    }

    // ── LayoutPlaceRemoveCommand (0xe7) with 3 subcommands ───────────────────
    func buildLayoutDisplayCmd(grayscale: [UInt8]) -> [UInt8] {
        let compressed = deflateCompress(grayscale)
        log("   Compressed: \(grayscale.count)B → \(compressed.count)B (\(Int(Double(compressed.count)/Double(grayscale.count)*100))%)", color: CLR_CYN)

        var sub1: [UInt8] = [0x01, 0x00, 0x0a]  // PLACE_STATE, len=10
        sub1 += [0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00]

        var sub2: [UInt8] = [0x03, 0x00, 0x18]  // PLACE_IMGOBJ, len=24
        sub2 += [0x00,0x00, 0x00, 0x00]         // objId=0, layerId=0, pad
        sub2 += [0x00,0x00,0x00,0x00]           // x=0
        sub2 += [0x00,0x00,0x00,0x00]           // y=0
        sub2 += [0x01,0xa3]                     // width=419
        sub2 += [0x00,0x8a]                     // height=138
        sub2 += [0x00, 0x00,0x00,0x00,0x00, 0x00, 0x00,0x00]  // flags + subtype + loadingId

        let imgDataLen = 2 + 1 + compressed.count  // objId(2)+fmt(1)+data
        var sub3: [UInt8] = [0x07,
                             UInt8((imgDataLen >> 8) & 0xff),
                             UInt8(imgDataLen & 0xff)]
        sub3 += [0x00,0x00]  // objId=0
        sub3 += [0x01]       // imgFormat=1 (8-bit mono + DEFLATE)
        sub3 += compressed

        let totalPayload = sub1.count + sub2.count + sub3.count
        var cmd: [UInt8] = [0xe7,
                            UInt8((totalPayload >> 8) & 0xff),
                            UInt8(totalPayload & 0xff)]
        cmd += sub1; cmd += sub2; cmd += sub3
        return cmd
    }

    // DEFLATE compress — native zlib, raw wbits=-15 (Java Deflater nowrap=true equivalent)
    func deflateCompress(_ input: [UInt8]) -> [UInt8] {
        let t0 = Date().timeIntervalSince1970 * 1000.0
        var strm = z_stream()
        let rc = deflateInit2_(&strm, Z_BEST_COMPRESSION, Z_DEFLATED, -15, 9,
                               Z_DEFAULT_STRATEGY, ZLIB_VERSION,
                               Int32(MemoryLayout<z_stream>.size))
        guard rc == Z_OK else { log("⚠️ deflateInit2 failed \(rc)", color: CLR_RED); return input }
        defer { deflateEnd(&strm) }
        let bufSize = input.count + 1024
        var buf = [UInt8](repeating: 0, count: bufSize)
        var inCopy = input
        inCopy.withUnsafeMutableBufferPointer { inPtr in
            strm.next_in  = inPtr.baseAddress
            strm.avail_in = UInt32(input.count)
        }
        buf.withUnsafeMutableBufferPointer { outPtr in
            strm.next_out  = outPtr.baseAddress
            strm.avail_out = UInt32(bufSize)
            _ = deflate(&strm, Z_FINISH)
        }
        let result = Array(buf[0..<Int(strm.total_out)])
        let ms = Int(Date().timeIntervalSince1970 * 1000.0 - t0)
        let ratio = input.isEmpty ? 0.0 : Double(result.count) / Double(input.count)
        emitEvent("COMPRESS", ["raw": input.count, "compressed": result.count,
                               "ratio": ratio, "ms": ms])
        return result
    }

    // ── REPL help ─────────────────────────────────────────────────────────────
    func printREPLHelp() {
        print("""
        \(CLR_CYN)
        ═══ INTERACTIVE MODE ═══\(CLR_RST)

        \(CLR_GRN)Display:\(CLR_RST)
          start   linit   white   black   checker   stripes   cross
          on   off   mode N   clear   screen N   gol

        \(CLR_GRN)WiFi (30+ fps path):\(CLR_RST)
          wifi on          — enable glasses WiFi radio
          wifi connect     — start TCP server + send WifiConnectReq
          wifi switch      — activate WiFi data path for frames
          wifi bt          — fall back to BT
          wifi             — show full WiFi help

        \(CLR_GRN)Other:\(CLR_RST)
          raw HEX   help   quit
        \(CLR_YLW)↑↑↑ LOOK AT THE GLASSES AFTER EACH DISPLAY COMMAND ↑↑↑\(CLR_RST)
        """)
        print("\(CLR_MAG)> \(CLR_RST)", terminator: "")
        fflush(stdout)
    }

    // ── REPL command handler ──────────────────────────────────────────────────
    func handleREPLCommand(_ line: String) {
        let parts = line.trimmingCharacters(in: .whitespacesAndNewlines)
                        .split(separator: " ")
        guard let cmdRaw = parts.first else {
            print("\(CLR_MAG)> \(CLR_RST)", terminator: ""); fflush(stdout); return
        }
        let cmd = cmdRaw.lowercased()

        switch cmd {

        // ── Display commands ──────────────────────────────────────────────────
        case "start":
            sendCmd([0x30, 0x00, 0x00], label: "OpenAppStartRequest")

        case "on":
            sendCmd([0xe9, 0x00, 0x01, 0x01], label: "DisplayTurnOn")

        case "off":
            sendCmd([0xe9, 0x00, 0x01, 0x00], label: "DisplayTurnOff")

        case "mode":
            let m = parts.count > 1 ? (UInt8(parts[1]) ?? 0) : 0
            sendCmd([0xc3, 0x00, 0x01, m], label: "OpenAppMode(\(m))")

        case "clear":
            sendCmd([0xb1, 0x00, 0x00], label: "OpenAppClearScreen")

        case "screen":
            let s = parts.count > 1 ? (UInt8(parts[1]) ?? 1) : 1
            sendCmd([0x3d, 0x00, 0x01, s], label: "SetScreenState(\(s))")

        case "linit":
            sendCmd([0xe0, 0x00, 0x0a,
                     0x00,0x00,0x00,0x00,
                     0x00,0x00,0x00,0x00,
                     0x00,0x00], label: "LayoutInit(0,0,state=0)")

        case "white":
            let gray = [UInt8](repeating: 0xFF, count: W * H)
            sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT all-white")

        case "black":
            let gray = [UInt8](repeating: 0x00, count: W * H)
            sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT all-black")

        case "checker":
            var gray = [UInt8](repeating: 0, count: W * H)
            for y in 0..<H { for x in 0..<W {
                if (x/16 + y/16) % 2 == 0 { gray[y*W+x] = 255 }
            }}
            sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT checker")

        case "stripes":
            var gray = [UInt8](repeating: 0, count: W * H)
            for y in 0..<H { if y % 8 < 4 { for x in 0..<W { gray[y*W+x] = 255 } } }
            sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT stripes")

        case "cross":
            var gray = [UInt8](repeating: 0, count: W * H)
            let cx = W/2, cy = H/2
            for x in 0..<W { gray[cy*W+x] = 255 }
            for y in 0..<H { gray[y*W+cx] = 255 }
            for x in 0..<W { gray[x] = 255; gray[(H-1)*W+x] = 255 }
            for y in 0..<H { gray[y*W] = 255; gray[y*W+W-1] = 255 }
            sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT cross")

        case "glider":
            log("🛸 Glider demo starting!", color: CLR_MAG)
            golStartGliderDemo()

        case "stop":
            golTimer?.invalidate(); golTimer = nil
            let black = [UInt8](repeating: 0, count: W * H)
            sendCmd(buildLayoutDisplayCmd(grayscale: black), label: "STOP — black frame")
            log("⏹  Demo stopped. Type 'glider' to restart.", color: CLR_YLW)

        // ── WiFi commands ─────────────────────────────────────────────────────
        case "wifi":
            let subcmd = parts.count > 1 ? String(parts[1]).lowercased() : ""
            switch subcmd {
            case "on":
                sendCmd([0x92, 0x00, 0x00], label: "WifiStatusTurnOnReq")
                wifiPhase = 10
                emitState()
                log("📶 Sent WifiTurnOnReq (0x92). Waiting for 0x91 ENABLED...", color: CLR_YLW)
            case "off":
                sendCmd([0x93, 0x00, 0x00], label: "WifiStatusTurnOffReq")
                wifiPhase = 0; wifiActive = false
                emitState()
            case "status":
                sendCmd([0x90, 0x00, 0x00], label: "WifiStatusReq")
            case "connect":
                // wifi connect auto  — use .env creds + en0 IP
                // wifi connect <ssid> <pass> <ip>
                if parts.count > 2 && String(parts[2]) == "auto" {
                    let ssid = config.wifiSSID.isEmpty ? wifiSSID : config.wifiSSID
                    let pass = config.wifiPSWD.isEmpty ? wifiPass : config.wifiPSWD
                    let ip   = getInterfaceIP("en0") ?? ""
                    guard !ssid.isEmpty, !pass.isEmpty else {
                        log("⚠️  No SSID/PSWD in .env. Use: wifi connect <ssid> <pass> <ip>", color: CLR_RED)
                        break
                    }
                    guard !ip.isEmpty else {
                        log("⚠️  Could not detect en0 IP. Are you on WiFi?", color: CLR_RED)
                        log("   Run: ipconfig getifaddr en0", color: CLR_YLW)
                        log("   Then: wifi connect \(ssid) \(pass) <your-ip>", color: CLR_YLW)
                        break
                    }
                    log("🔑 Auto: SSID=\(ssid)  IP=\(ip)", color: CLR_CYN)
                    wifiStartConnect(ssid: ssid, pass: pass, goIP: ip)
                } else {
                    let ssid = parts.count > 2 ? String(parts[2]) : wifiSSID
                    let pass = parts.count > 3 ? String(parts[3]) : wifiPass
                    let ip   = parts.count > 4 ? String(parts[4]) : wifiGoIP
                    wifiStartConnect(ssid: ssid, pass: pass, goIP: ip)
                }
            case "switch":
                if wifiClientFd < 0 {
                    log("⚠️ No TCP connection yet. Wait for glasses to connect.", color: CLR_YLW)
                } else {
                    sendCmd([0x96, 0x00, 0x01, 0x01], label: "WifiDPSwitchPathReq(WIFI)")
                    log("🔀 WifiDPSwitchPathReq(WIFI) sent. Waiting for 0x97...", color: CLR_YLW)
                }
            case "bt":
                sendCmd([0x96, 0x00, 0x01, 0x00], label: "WifiDPSwitchPathReq(BT)")
                wifiActive = false; wifiPhase = 0
                log("⬅️ Switched back to BT data path.", color: CLR_YLW)
            case "ip":
                detectWifiIP()
                print("\(CLR_MAG)> \(CLR_RST)", terminator: ""); fflush(stdout); return
            case "setup":
                printWifiSetup(); return
            case "ssid":
                if parts.count > 2 { wifiSSID = String(parts[2]) }
                log("SSID set to: \(wifiSSID)", color: CLR_CYN)
            case "pass":
                if parts.count > 2 { wifiPass = String(parts[2]) }
                log("Pass set to: \(wifiPass)", color: CLR_CYN)
            case "goip":
                if parts.count > 2 { wifiGoIP = String(parts[2]) }
                log("macOS IP set to: \(wifiGoIP)", color: CLR_CYN)
            default:
                printWifiHelp(); return
            }

        // ── Other ─────────────────────────────────────────────────────────────
        case "camera", "cam":
            let subcmd = parts.count > 1 ? String(parts[1]).lowercased() : "help"
            switch subcmd {
            case "still":
                // RE STATUS: camera goes through Android Intents → MisiAha daemon → RFCOMM.
                // Raw wire bytes unknown. Need HCI snoop from the SmartEyeglass APK.
                // Attempting known camera-range commands as RE probes:
                log("📷 Camera RE probe: trying 0xc3 mode commands...", color: CLR_CYN)
                // 0xc3 = OpenAppMode — try camera mode value 4 (POWER_MODE_HIGH per SDK)
                sendCmd([0xc3, 0x00, 0x01, 0x04], label: "SetPowerMode(HIGH/camera?)")
                sendCmd([0xc3, 0x00, 0x01, 0x00], label: "SetPowerMode(0)")
                log("⚠️  Camera bytes not yet reverse-engineered.", color: CLR_YLW)
                log("   TODO: HCI snoop trace from com.sony.smarteyeglass APK", color: CLR_YLW)
                log("   See: glasses-sdk/PROTOCOL_MAP.md §Camera", color: CLR_YLW)
            case "stop":
                log("📷 Camera stop (stub — no-op until bytes RE'd)", color: CLR_YLW)
            default:
                print("""
                \(CLR_CYN)
                ═══ CAMERA ═══
                Status: RE in progress — wire bytes unknown
                The camera is QVGA JPEG, accessed via Android Intents
                on the device. From macOS over BT, we need the raw
                RFCOMM command bytes which require an HCI snoop.

                \(CLR_YLW)camera still\(CLR_RST)  — try RE probe commands
                \(CLR_YLW)camera stop\(CLR_RST)   — stop camera (stub)
                \(CLR_RST)Next step: adb shell btsnoopenable + capture from APK\(CLR_RST)
                """)
            }

        case "raw":
            let hexStr = parts.dropFirst().joined(separator: "")
            if let data = hexToData(hexStr) {
                sendCmd(Array(data), label: "RAW \(data.count)B")
            } else {
                log("Invalid hex. Usage: raw e9 00 01 01", color: CLR_RED)
            }

        case "help", "h", "?":
            printREPLHelp(); return

        case "quit", "exit", "q":
            log("👋 Disconnecting...", color: CLR_YLW)
            if wifiServerFd >= 0 { Darwin.close(wifiServerFd) }
            if wifiClientFd >= 0 { Darwin.close(wifiClientFd) }
            gChannel?.close()
            exit(0)

        default:
            log("Unknown command: \(cmd). Type 'help'.", color: CLR_RED)
        }

        print("\(CLR_MAG)> \(CLR_RST)", terminator: "")
        fflush(stdout)
    }

    // ── GoL frame sender (BT or WiFi) ─────────────────────────────────────────
    func golSendFrame() {
        let gray = golToImage()
        let cmd  = buildLayoutDisplayCmd(grayscale: gray)
        if wifiActive && wifiClientFd >= 0 {
            sendViaTCP(cmd, label: "GOL-WiFi gen=\(golGeneration)")
        } else {
            sendCmd(cmd, label: "GOL gen=\(golGeneration)")
        }
    }


    // ── Gosper Glider Gun seed ────────────────────────────────────────────────
    // The canonical 36-cell pattern that continuously produces gliders.
    // Coordinates are (x,y) offsets from the gun's top-left corner.
    let gosperGunCells: [(Int,Int)] = [
        (24,0),
        (22,1),(24,1),
        (12,2),(13,2),(20,2),(21,2),(34,2),(35,2),
        (11,3),(15,3),(20,3),(21,3),(34,3),(35,3),
        (0,4),(1,4),(10,4),(16,4),(20,4),(21,4),
        (0,5),(1,5),(10,5),(14,5),(16,5),(17,5),(22,5),(24,5),
        (10,6),(16,6),(24,6),
        (11,7),(15,7),
        (12,8),(13,8)
    ]

    func placeGosperGun(ox: Int, oy: Int, flipX: Bool = false) {
        let maxX = 35
        for (dx, dy) in gosperGunCells {
            let fx = flipX ? (maxX - dx) : dx
            let x = ox + fx; let y = oy + dy
            if x >= 1 && x < W-1 && y >= 1 && y < H-11 { grid[y][x] = true }
        }
    }

    func golStartGliderDemo() {
        golTimer?.invalidate(); golTimer = nil
        golInit()

        // Two guns facing each other across the display — creates rich collisions
        placeGosperGun(ox: 10,  oy: 15)               // shoots right
        placeGosperGun(ox: 373, oy: 55, flipX: true)  // shoots left

        // R-pentomino near center for extra complexity (tiny but long-lived)
        let cx = W/2; let cy = 40
        for (dx,dy) in [(1,0),(2,0),(0,1),(1,1),(1,2)] {
            let x = cx+dx; let y = cy+dy
            if x >= 1 && x < W-1 && y >= 1 && y < H-11 { grid[y][x] = true }
        }

        golGeneration = 0
        golSendFrame()

        // 30fps when WiFi active, ~2.5fps over BT
        let interval: Double = (wifiActive && wifiClientFd >= 0) ? (1.0/30.0) : 0.4
        golTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { _ in
            golStep()
            golGeneration += 1
            golSendFrame()
        }
        RunLoop.current.add(golTimer!, forMode: .default)
    }

    // ── Wire up delegates ─────────────────────────────────────────────────────
    gDelegate?.onOpen = { channel in
        log("RFCOMM open ch\(rfcommChannel) MTU=\(channel.getMTU())", color: CLR_GRN)
        log("Handshake starting: waiting for ProtocolVersion...", color: CLR_YLW)
        DispatchQueue.global(qos: .userInitiated).async {
            while let line = readLine() {
                DispatchQueue.main.async { handleREPLCommand(line) }
            }
        }
    }

    gDelegate?.onData = { data in
        rxCount += data.count
        gCapture?.write(direction: " RX(BT)", data: data)
        rxBuf.append(data)

        // Consume all complete frames from rxBuf
        while rxBuf.count >= 3 {
            let si    = rxBuf.startIndex
            let fLen  = (Int(rxBuf[si + 1]) << 8) | Int(rxBuf[si + 2])
            let fTotal = 3 + fLen
            guard rxBuf.count >= fTotal else { break }   // partial frame — wait for more
            let frame = Data(rxBuf[si ..< (si + fTotal)])
            rxBuf     = Data(rxBuf[(si + fTotal)...])    // advance past this frame

            // Shadow outer `data` so every existing reference below works unchanged
            let data   = frame
            let cmdId  = data[0]

        // Emit RX event for every incoming command
        let rxNames: [UInt8: String] = [
            0x01: "ACK", 0x02: "NAK", 0x05: "PING", 0x06: "LevelNotification",
            0x08: "VersionResponse", 0x0a: "ProtocolVersion", 0x72: "SettingsStatusResponse",
            0x81: "FotaStatus", 0x91: "WifiStatusRes", 0x95: "WifiConnectivityStatus",
            0x96: "WifiDPSwitchPathReq", 0x97: "WifiDPSwitchPathRes",
            0xe5: "LayoutEventNotify", 0xe8: "ImageAck", 0xff: "SyncResponse"
        ]
        let rxCmdHex = String(format: "0x%02x", cmdId)
        let rxCmdName = rxNames[cmdId] ?? rxCmdHex
        let rxPayload = data.count > 3 ? data[3...].prefix(8).map{String(format:"%02x",$0)}.joined() : ""
        emitEvent("RX", ["cmd": rxCmdHex, "name": rxCmdName, "payload": rxPayload, "phase": initPhase])

        switch (initPhase, cmdId) {

        // ── BT handshake ──────────────────────────────────────────────────────
        case (0, 0x0a):
            initPhase = 1
            emitState()
            log("✨ P1: ProtocolVersion. Sending SettingsStatusRequest...", color: CLR_MAG)
            sendCmd([0x71, 0x00, 0x00], label: "SettingsStatusRequest")

        case (1, 0x72):
            initPhase = 2
            emitState()
            log("✨ P2: SettingsStatusResponse. Sending VersionRequest...", color: CLR_MAG)
            sendCmd([0x07, 0x00, 0x01, 0x01], label: "VersionRequest")

        case (2, 0x08):
            initPhase = 3
            emitState()
            let ver = data.count > 3 ? (String(bytes: Array(data[3...]), encoding: .ascii) ?? "?") : "?"
            log("✨ P3: FW=\(ver). Sending NewHostApp...", color: CLR_MAG)
            sendCmd([0x85, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00], label: "NewHostApp(0)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) {
                guard initPhase == 3 else { return }
                log("⏳ No FotaStatus after 5s — advancing...", color: CLR_YLW)
                initPhase = 4
                sendCmd([0x30, 0x00, 0x00], label: "OpenAppStartRequest")
                log("👉 TAP the glasses touch sensor to confirm!", color: CLR_YLW)
            }

        case (3, 0x81):
            log("✨ P3: FotaStatus. Sending SyncResponse (critical!)...", color: CLR_MAG)
            sendCmd([0xff, 0x00, 0x00], label: "SyncResponse")
            initPhase = 4
            emitState()
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", color: CLR_YLW)
            log("👓 Tap the touch sensor OR type 'help' for commands.", color: CLR_YLW)
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", color: CLR_YLW)

        case (3, _):
            log("✨ P3: 0x\(String(format:"%02x",cmdId)) (waiting for FotaStatus)", color: CLR_YLW)

        case (4, 0x31):
            log("🎉 P4→5: OpenAppStartResponse! Glasses confirmed!", color: CLR_GRN)
            initPhase = 5
            emitState()
            sendCmd([0xe0,0x00,0x0a,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00],
                    label: "LayoutInit(0,0,state=0)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                let gray = [UInt8](repeating: 0xFF, count: W * H)
                sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT all-white")
                if gDisplayMode == .test { printREPLHelp() }
                else { printReadyBanner() }
            }
            if autoWifi { triggerAutoWifi() }

        case (4, 0x06):
            let level = data.count > 3 ? data[3] : 0
            log("🎉 P4→5: LevelNotification(level=\(level))! Glasses READY!", color: CLR_GRN)
            initPhase = 5
            emitState()
            sendCmd([0x30, 0x00, 0x00], label: "OpenAppStartRequest")
            sendCmd([0xe0,0x00,0x0a,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00],
                    label: "LayoutInit(0,0,state=0)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                let gray = [UInt8](repeating: 0xFF, count: W * H)
                sendCmd(buildLayoutDisplayCmd(grayscale: gray), label: "LAYOUT all-white")
                if gDisplayMode == .test { printREPLHelp() }
                else { printReadyBanner() }
            }
            if autoWifi { triggerAutoWifi() }

        case (4, 0x81):
            log("✨ P4: FotaStatus (ignoring — waiting for LevelNotification)", color: CLR_YLW)

        case (4, 0xe5):
            break  // LayoutEventNotify — suppress flood

        case (4, _):
            log("✨ P4: 0x\(String(format:"%02x",cmdId))", color: CLR_YLW)

        // ── WiFi responses (any phase, before phase-5 catch-all) ─────────────

        case (_, 0x91): // WifiStatusRes
            let status = data.count > 3 ? data[3] : 0xFF
            let statusNames = ["DISABLING","DISABLED","ENABLING","ENABLED","UNKNOWN"]
            let sName = Int(status) < statusNames.count ? statusNames[Int(status)] : "?"
            log("📶 WifiStatusRes(0x91): \(sName) (\(status))", color: status == 3 ? CLR_GRN : CLR_MAG)
            if status == 3 && wifiPhase == 10 {
                log("✅ Glasses WiFi ENABLED.", color: CLR_GRN)
                wifiPhase = 11
                emitEvent("WIFI", ["event": "ENABLED", "state": Int(status)])
                emitState()
                if autoWifi {
                    let ssid = config.wifiSSID; let pass = config.wifiPSWD
                    let ip   = getInterfaceIP("en0") ?? ""
                    guard !ip.isEmpty else { log("⚠️ Auto WiFi: no en0 IP", color: CLR_YLW); return }
                    log("🌐 Auto: connecting to '\(ssid)'...", color: CLR_CYN)
                    wifiStartConnect(ssid: ssid, pass: pass, goIP: ip)
                }
            }

        case (_, 0x95): // WifiConnectivityStatus
            let status = data.count > 3 ? data[3] : 0xFF
            let statusNames = ["DISCONNECTING","DISCONNECTED","CONNECTING","CONNECTED","UNKNOWN"]
            let sName = Int(status) < statusNames.count ? statusNames[Int(status)] : "?"
            log("📡 WifiConnectivityStatus(0x95): \(sName) (\(status))",
                color: status == 3 ? CLR_GRN : CLR_MAG)
            if status == 3 {
                log("✅ Glasses joined WiFi! Waiting for TCP connection on port \(wifiPort)...", color: CLR_GRN)
                log("   (TCP accept is already running in background)", color: CLR_YLW)
                emitEvent("WIFI", ["event": "CONNECTED", "state": Int(status)])
                emitState()
            }

        case (_, 0x97): // WifiDPSwitchPathRes
            let path = data.count > 3 ? data[3] : 0
            log("🔀 WifiDPSwitchPathRes(0x97): path=\(path == 1 ? "WIFI" : "BT")", color: CLR_GRN)
            if path == 1 {
                wifiActive = true; wifiPhase = 13
                emitEvent("WIFI", ["event": "SWITCHED", "state": 13])
                emitState()
                log("🚀 WiFi data path ACTIVE. Type 'glider' to start 30fps demo.", color: CLR_GRN)
                printReadyBanner()
            } else {
                wifiActive = false; wifiPhase = 0
                emitState()
                log("⬅️ Data path back to BT.", color: CLR_YLW)
            }

        case (_, 0x96): // WifiDPSwitchPathReq FROM glasses (glasses requesting BT fallback)
            let path = data.count > 3 ? data[3] : 0
            log("🔀 WifiDPSwitchPathReq(0x96) from glasses: path=\(path)", color: CLR_YLW)
            if path == 0 {
                wifiActive = false; wifiPhase = 0
                log("⬅️ Glasses switched us back to BT path.", color: CLR_YLW)
            }

        // ── Phase 5 general ───────────────────────────────────────────────────
        case (5, _):
            let cmdName: String
            switch cmdId {
            case 0x01: cmdName = "ACK"
            case 0x02: cmdName = "NAK"
            case 0x05: cmdName = "PING"
            case 0x06: cmdName = "LEVEL_NOTIFICATION"
            case 0x32: cmdName = "TOUCH"
            case 0x36: cmdName = "IMAGE_ACK"
            case 0x3c: cmdName = "KEY_EVENT"
            default: cmdName = String(format: "CMD_0x%02x", cmdId)
            }
            log("   [\(cmdName)]", color: CLR_CYN)

        case (_, 0x0a) where initPhase > 0:
            initPhase = 1
            log("✨ Re-received ProtocolVersion. Restarting handshake...", color: CLR_MAG)
            sendCmd([0x71, 0x00, 0x00], label: "SettingsStatusRequest")

        default:
            log("   [phase=\(initPhase) cmd=0x\(String(format:"%02x",cmdId))]", color: CLR_YLW)
        }

        // Print RX dump AFTER switch (so phase-transition logs appear first)
        log("← RX \(data.count)B (total \(rxCount)):", color: CLR_GRN)
        print(data.hexDump)
        } // end while — next complete frame
    }

    gDelegate?.onClose = {
        log("Disconnected after \(rxCount) bytes", color: CLR_RED)
        gCapture?.close()
        exit(0)
    }

    var tempChannel: IOBluetoothRFCOMMChannel?
    let result = device.openRFCOMMChannelAsync(&tempChannel,
                                               withChannelID: rfcommChannel,
                                               delegate: gDelegate)
    guard result == kIOReturnSuccess else {
        log("openRFCOMMChannelAsync failed: 0x\(String(format: "%08x", result))", color: CLR_RED)
        log("  • Glasses not paired with this Mac?", color: CLR_YLW)
        log("  • Try: ./glasses-tool probe \(address)", color: CLR_YLW)
        return
    }
    log("RFCOMM connection in progress...", color: CLR_CYN)
    RunLoop.current.run()
    gCapture?.close()
}

// MARK: probe
func cmdProbe(address: String) {
    log("Probing RFCOMM channels 1-10 on \(address)...", color: CLR_CYN)
    guard let device = IOBluetoothDevice(addressString: address) else {
        log("Device not found: \(address)", color: CLR_RED); return
    }
    device.performSDPQuery(nil)
    RunLoop.current.run(until: Date().addingTimeInterval(4))
    for ch in UInt8(1)...UInt8(10) {
        var channelObj: IOBluetoothRFCOMMChannel?
        let result = device.openRFCOMMChannelSync(&channelObj, withChannelID: ch, delegate: nil)
        if result == kIOReturnSuccess, let ch_obj = channelObj {
            log("  ✅ Channel \(ch): OPEN (MTU=\(ch_obj.getMTU()))", color: CLR_GRN)
            ch_obj.close()
        } else {
            log("  ✗  Channel \(ch): refused (0x\(String(format: "%x", result)))", color: CLR_RST)
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.3))
    }
}

// MARK: pair
func cmdPairGuide() {
    // List currently paired SmartEyeglass devices so the user knows what's already known
    let paired = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
    let glasses = paired.filter { ($0.name ?? "").lowercased().contains("smarteyeglass") }

    print("""
    \(CLR_CYN)
    ╔══════════════════════════════════════════════════════════╗
    ║   Sony SED-E1 — Pairing a new device                    ║
    ╠══════════════════════════════════════════════════════════╣
    ║  1. Slide POWER switch, hold 4+ sec → text on lenses    ║
    ║  2. System Settings → Bluetooth → find SmartEyeglass    ║
    ║     → Connect → tap touch sensor to confirm             ║
    ║  3. Run: ./glasses-tool                                  ║
    ║     (auto-discovers any paired SmartEyeglass)            ║
    ╚══════════════════════════════════════════════════════════╝
    \(CLR_RST)
    """)

    if glasses.isEmpty {
        log("No SmartEyeglass paired yet — follow steps above.", color: CLR_YLW)
    } else {
        log("Already paired (\(glasses.count)):", color: CLR_GRN)
        for d in glasses {
            log("  ● \(d.name ?? "?")  [\(d.addressString ?? "?")]", color: CLR_GRN)
        }
        log("Run \(CLR_GRN)./glasses-tool\(CLR_RST) to connect.", color: CLR_RST)
    }
}

// ── Config file ───────────────────────────────────────────────────────────────
struct GlassesConfig {
    var btAddress: String = "auto"          // "auto" = scan for any paired SmartEyeglass
    var rfcommChannel: BluetoothRFCOMMChannelID = 4
    var captureLog: String = "/tmp/glasses_capture.log"
    var wifiSSID: String = ""
    var wifiPSWD: String = ""

    static func load() -> GlassesConfig {
        var cfg = GlassesConfig()
        let binDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().path
        let cwd    = FileManager.default.currentDirectoryPath

        // glasses.conf
        for path in [binDir + "/glasses.conf", cwd + "/glasses.conf", "glasses.conf"] {
            guard let content = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
            log("📋 Config: \(path)", color: CLR_CYN)
            for line in content.components(separatedBy: .newlines) {
                let t = line.trimmingCharacters(in: .whitespaces)
                guard !t.isEmpty, !t.hasPrefix("#") else { continue }
                let p = t.split(separator: "=", maxSplits: 1)
                guard p.count == 2 else { continue }
                let k = p[0].trimmingCharacters(in: .whitespaces)
                let v = p[1].trimmingCharacters(in: .whitespaces)
                switch k {
                case "bt_address":     cfg.btAddress = v
                case "rfcomm_channel": cfg.rfcommChannel = BluetoothRFCOMMChannelID(v) ?? 4
                case "capture_log":    cfg.captureLog = v
                case "wifi_ssid":      cfg.wifiSSID = v
                case "wifi_pswd":      cfg.wifiPSWD = v
                default: break
                }
            }
            break
        }

        // .env (SSID= / PSWD= keys)
        for path in [binDir + "/.env", cwd + "/.env", ".env"] {
            guard let content = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
            for line in content.components(separatedBy: .newlines) {
                let t = line.trimmingCharacters(in: .whitespaces)
                guard !t.isEmpty, !t.hasPrefix("#") else { continue }
                let p = t.split(separator: "=", maxSplits: 1)
                guard p.count == 2 else { continue }
                let k = p[0].trimmingCharacters(in: .whitespaces)
                let v = p[1].trimmingCharacters(in: .whitespaces)
                switch k {
                case "SSID": if cfg.wifiSSID.isEmpty { cfg.wifiSSID = v }
                case "PSWD": if cfg.wifiPSWD.isEmpty { cfg.wifiPSWD = v }
                default: break
                }
            }
            break
        }
        return cfg
    }
}


// ── Last-used device registry ─────────────────────────────────────────────────
// Persists the most-recently connected addresses to ~/.glasses-last (one per line).
// On next run the scan is skipped if the last-used device is already paired.

func lastUsedPath() -> String {
    (ProcessInfo.processInfo.environment["HOME"] ?? "/tmp") + "/.glasses-last"
}

func loadLastUsed() -> [String] {
    guard let txt = try? String(contentsOfFile: lastUsedPath(), encoding: .utf8) else { return [] }
    return txt.components(separatedBy: .newlines)
              .map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
              .filter { !$0.isEmpty }
}

func saveLastUsed(_ addr: String) {
    let norm = addr.lowercased()
    var list = loadLastUsed().filter { $0 != norm }
    list.insert(norm, at: 0)
    let out = list.prefix(10).joined(separator: "\n") + "\n"
    try? out.write(toFile: lastUsedPath(), atomically: true, encoding: .utf8)
}

// ── Auto-discover paired SmartEyeglass ───────────────────────────────────────
func scanAndSelectGlasses() -> String? {
    let lastUsed = loadLastUsed()
    let allPaired = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
    let pairedGlasses = allPaired.filter { ($0.name ?? "").lowercased().contains("smarteyeglass") }

    // Fast path: last-used address is already in paired list — skip scan entirely
    for addr in lastUsed {
        if let d = pairedGlasses.first(where: { $0.addressString?.lowercased() == addr }) {
            log("✅ Last-used \(d.name ?? "SmartEyeglass") [\(d.addressString ?? "?")] (from history — skipping scan)", color: CLR_GRN)
            return d.addressString
        }
    }

    // Fast path: exactly one SmartEyeglass paired and none in history — auto-pick
    if pairedGlasses.count == 1 && lastUsed.isEmpty {
        let d = pairedGlasses[0]
        log("✅ Only paired SmartEyeglass: \(d.name ?? "?") [\(d.addressString ?? "?")] — auto-connecting", color: CLR_GRN)
        return d.addressString
    }

    // Need to scan
    log("🔍 Scanning for SmartEyeglass... (8s — power on glasses now)", color: CLR_CYN)
    for d in pairedGlasses {
        log("  ★ \(d.name ?? "?")  [\(d.addressString ?? "")]  (paired)", color: CLR_MAG)
    }

    var found: [IOBluetoothDevice] = pairedGlasses
    var seen = Set(pairedGlasses.compactMap { $0.addressString?.lowercased() })

    let inquiry = IOBluetoothDeviceInquiry(delegate: nil)!
    inquiry.inquiryLength = 8
    inquiry.start()

    var dots = 0
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        dots += 1
        if dots % 2 == 0 { print(".", terminator: ""); fflush(stdout) }
        if let devices = inquiry.foundDevices() as? [IOBluetoothDevice] {
            for d in devices {
                let addr = d.addressString?.lowercased() ?? ""
                guard !seen.contains(addr) else { continue }
                seen.insert(addr)
                if (d.name ?? "").lowercased().contains("smarteyeglass") {
                    found.append(d)
                    print(""); log("  + \(d.name ?? "?")  [\(d.addressString ?? "")]  (found)", color: CLR_GRN)
                }
            }
        }
    }
    inquiry.stop()
    print("")

    // No SmartEyeglass found — intelligent suggestion by Sony MAC OUI
    if found.isEmpty {
        let sonyOUI = ["ac:9b:0a", "20:16:d8", "e0:f0:10"]
        let scanned = (inquiry.foundDevices() as? [IOBluetoothDevice]) ?? []
        let suggestions = (allPaired + scanned).filter { d in
            let addr = d.addressString?.lowercased() ?? ""
            return sonyOUI.contains(where: { addr.hasPrefix($0) })
        }
        log("❌ No SmartEyeglass found.", color: CLR_RED)
        log("   → Hold power switch 4+ sec to power on", color: CLR_YLW)
        log("   → Pair first: System Settings → Bluetooth", color: CLR_YLW)
        log("   → Or: ./glasses-tool connect <address>", color: CLR_YLW)
        if !suggestions.isEmpty {
            log("   → Possible match by Sony manufacturer ID:", color: CLR_CYN)
            for d in suggestions {
                log("       \(d.name ?? "(unknown)")  [\(d.addressString ?? "?")]", color: CLR_CYN)
            }
        }
        return nil
    }

    // After scan: if a last-used address showed up, use it
    for addr in lastUsed {
        if let d = found.first(where: { $0.addressString?.lowercased() == addr }) {
            log("✅ Last-used \(d.name ?? "?") [\(d.addressString ?? "?")] (matched after scan)", color: CLR_GRN)
            return d.addressString
        }
    }

    // Exactly one candidate
    if found.count == 1 {
        let d = found[0]
        log("✅ Connecting to \(d.name ?? "?") [\(d.addressString ?? "?")]", color: CLR_GRN)
        return d.addressString
    }

    // Multiple: sort last-used first, show with recommendation
    let sorted = found.sorted { a, b in
        let ai = lastUsed.firstIndex(of: a.addressString?.lowercased() ?? "") ?? Int.max
        let bi = lastUsed.firstIndex(of: b.addressString?.lowercased() ?? "") ?? Int.max
        return ai < bi
    }
    log("Found \(sorted.count) SmartEyeglass devices:", color: CLR_CYN)
    for (i, d) in sorted.enumerated() {
        let addr = d.addressString?.lowercased() ?? ""
        let tag  = lastUsed.contains(addr) ? CLR_GRN + " ← last used" + CLR_RST : ""
        log("  [\(i+1)] \(d.name ?? "?")  [\(d.addressString ?? "?")]\(tag)", color: CLR_CYN)
    }
    print("\(CLR_YLW)Select [1-\(sorted.count)] (Enter = 1, recommended): \(CLR_RST)", terminator: "")
    fflush(stdout)
    if let line = readLine(),
       let n = Int(line.trimmingCharacters(in: .whitespaces)),
       n >= 1, n <= sorted.count {
        return sorted[n-1].addressString
    }
    return sorted[0].addressString
}

let config = GlassesConfig.load()

// Suppress internal IOBluetooth / os_log framework chatter (e.g. initWithDelegate: 0x0)
setenv("OS_ACTIVITY_MODE", "disable", 1)

// ── Entry point ───────────────────────────────────────────────────────────────
let args = CommandLine.arguments

func usage() {
    let ssidHint = config.wifiSSID.isEmpty ? "<ssid>" : config.wifiSSID
    let pswdHint = config.wifiPSWD.isEmpty ? "<pass>" : "***"
    print("""
    \(CLR_CYN)glasses-tool — Sony SED-E1 macOS BT+WiFi\(CLR_RST)

    \(CLR_GRN)./glasses-tool\(CLR_RST)                 auto-discover + connect
    \(CLR_GRN)./glasses-tool scan\(CLR_RST)             discover nearby BT devices
    \(CLR_GRN)./glasses-tool pair\(CLR_RST)             pairing guide
    \(CLR_GRN)./glasses-tool connect [ADDR]\(CLR_RST)   connect to specific address
    \(CLR_GRN)./glasses-tool probe [ADDR]\(CLR_RST)     probe RFCOMM channels

    REPL commands (after BT connects):
      \(CLR_GRN)glider\(CLR_RST)   — start glider demo (BT ~2.5fps)
      \(CLR_GRN)stop\(CLR_RST)     — stop demo
      \(CLR_GRN)wifi on\(CLR_RST)  — enable glasses WiFi
      \(CLR_GRN)wifi connect \(ssidHint) \(pswdHint) <your-ip>\(CLR_RST)
      \(CLR_GRN)wifi connect auto\(CLR_RST)  — use .env credentials + en0 IP
      \(CLR_GRN)wifi switch\(CLR_RST)  — switch to WiFi (30fps glider auto-starts)
      \(CLR_GRN)wifi setup\(CLR_RST)   — print step-by-step WiFi instructions
    """)
}

func resolveAddress(_ explicit: String?) -> String? {
    let addr = explicit ?? config.btAddress
    if addr == "auto" || addr.isEmpty { return scanAndSelectGlasses() }
    return addr
}

if args.count < 2 {
    guard let addr = resolveAddress(nil) else { exit(1) }
    log("🔌 Connecting to \(addr) ch=\(config.rfcommChannel)...", color: CLR_CYN)
    cmdConnect(address: addr, channel: config.rfcommChannel, captureFile: config.captureLog)
} else {
    switch args[1] {
    case "scan":  cmdScan(); exit(0)
    case "pair":  cmdPairGuide(); exit(0)
    case "sdp":   cmdSDP(address: resolveAddress(args.count > 2 ? args[2] : nil) ?? ""); exit(0)
    case "probe": cmdProbe(address: resolveAddress(args.count > 2 ? args[2] : nil) ?? ""); exit(0)
    case "connect":
        guard let addr = resolveAddress(args.count > 2 ? args[2] : nil) else { exit(1) }
        var ch = config.rfcommChannel
        if let idx = args.firstIndex(of: "--channel"), args.count > idx + 1 {
            ch = BluetoothRFCOMMChannelID(args[idx + 1]) ?? ch
        }
        cmdConnect(address: addr, channel: ch, captureFile: config.captureLog)
    case "-h", "--help", "help":
        usage(); exit(0)
    default:
        if args[1].contains(":") {
            cmdConnect(address: args[1], channel: config.rfcommChannel,
                       captureFile: config.captureLog)
        } else { usage() }
    }
}

RunLoop.main.run()
