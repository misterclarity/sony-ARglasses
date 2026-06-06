# SED-E1 Agent Skill Reference

> Zero-context agent entry point. Dense tables + code.

---

## 1. Quick Start

```bash
# Build binary
cd /Users/gerhardgustav/Desktop/hobby-dev/sony-sed-e1/macos-middleware
swiftc glasses-tool.swift -framework IOBluetooth -framework Foundation -o glasses-tool -O

# Run TUI harness (glasses must be powered on + paired)
cd /Users/gerhardgustav/Desktop/hobby-dev/sony-sed-e1
node harness/dist/index.js

# Dev mode (no glasses)
node harness/dist/index.js --no-glasses
```

---

## 2. TUI Keyboard Reference

| Key | Sends to glasses-tool | Description |
|-----|----------------------|-------------|
| `w` | `wifi on` | Enable WiFi radio |
| `c` | `wifi connect auto` | Connect using .env creds + en0 IP |
| `s` | `wifi switch` | Activate WiFi data path (30fps) |
| `b` | `wifi bt` | Fall back to BT data path |
| `g` | `glider` | Start glider animation |
| `x` | `stop` | Stop + black frame |
| `G` | *(toggle)* | Toggle guardrail mode (ON = confirm all commands) |
| `?` | `help` | Print REPL help |
| `q` | `quit` | Graceful disconnect + exit |
| `Esc` | *(local)* | Clear composer input |
| `Enter` | *(send)* | Send composer content |
| `A` | *(guardrail)* | Allow pending command (when guardrail ON) |
| `S` | *(guardrail)* | Skip pending command |

---

## 3. REPL Command Reference (glasses-tool stdin)

| Command | Description |
|---------|-------------|
| `glider` | Start Game-of-Life glider animation |
| `stop` | Stop demo, send black frame |
| `white` / `black` / `checker` / `stripes` / `cross` | Test patterns |
| `wifi on` | Send WifiTurnOnReq (0x92) |
| `wifi off` | Send WifiTurnOffReq (0x93) |
| `wifi status` | Send WifiStatusReq (0x90) |
| `wifi connect auto` | TCP server + WifiConnectReq using .env + en0 IP |
| `wifi connect <ssid> <pass> <ip>` | WifiConnectReq with explicit args |
| `wifi switch` | WifiDPSwitchPathReq mode=WIFI (0x96 byte=0x01) |
| `wifi bt` | WifiDPSwitchPathReq mode=BT (0x96 byte=0x00) |
| `wifi setup` | Print step-by-step WiFi instructions |
| `wifi ip` | Detect macOS WiFi IP (en0) |
| `raw HEX` | Send raw bytes, e.g. `raw e9 00 01 01` |
| `help` | Full command list |
| `quit` | Disconnect + exit |

---

## 4. Full Protocol Reference

### 4.1 Handshake Sequence (BT RFCOMM, channel 4)

```
Dir  Cmd   Name                  Notes
←    0x0a  ProtocolVersion       First frame from glasses after connect
→    0x71  SettingsStatusReq     [0x71, 0x00, 0x00]
←    0x72  SettingsStatusRes
→    0x07  VersionReq            [0x07, 0x00, 0x01, 0x01]
←    0x08  VersionRes            Firmware version string
→    0x85  NewHostApp            [0x85, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00]
←    0x81  FotaStatus
→    0xff  SyncResponse          [0xff, 0x00, 0x00]  ← CRITICAL unlock
←    0x06  LevelNotification     User taps touch sensor → phase 4→5
     (or)
←    0x31  OpenAppStartResponse  Alternative phase 4→5 trigger
```

Phase numbers: 0=init 1=proto_ver 2=settings 3=version 4=new_host_app 5=display_ready

### 4.2 Display Commands

```
0xe0  LayoutInit  [0xe0,0x00,0x0a, 0,0,0,0, 0,0,0,0, 0,0]   viewX=0 viewY=0 state=0
0xe7  LayoutPlaceRemoveCommand  (multi-subcommand, see below)
```

**0xe7 subcommands (in order, concatenated):**
```
sub 0x01 PLACE_STATE   (10B): stateId=0, jog=0,0,0, options=0
sub 0x03 PLACE_IMGOBJ  (24B): objId=0, layerId=0, x=0, y=0, w=419(0x01a3), h=138(0x008a), sticky=0
sub 0x07 PLACE_IMGDATA (N B): objId=0, imgFormat=1, <DEFLATE bytes>
```

**Image format:**
- Size: 419×138 = 57822 bytes, 1 byte/pixel (0=black, 255=white), grayscale
- Compression: raw DEFLATE, wbits=-15 (no zlib header, Java nowrap=true)
  - Python: `zlib.compressobj(9, zlib.DEFLATED, -15)`
  - Swift: `deflateInit2_(..., -15, ...)`

### 4.3 WiFi Command Map

```
→ 0x90  WifiStatusReq           len=0  → query radio state
← 0x91  WifiStatusRes           payload[0]: 0=DISABLING 1=DISABLED 2=ENABLING 3=ENABLED
→ 0x92  WifiTurnOnReq           len=0
→ 0x93  WifiTurnOffReq          len=0
→ 0x94  WifiConnectReq          184B payload (see layout below)
← 0x95  WifiConnectivityStatus  payload[0]: 0=DISCONNECTING 1=DISCONNECTED 2=CONNECTING 3=CONNECTED
→ 0x96  WifiDPSwitchPathReq     [0x96,0x00,0x01, mode]  mode: 0x00=BT 0x01=WiFi
← 0x97  WifiDPSwitchPathRes     confirms path switch
```

**WifiConnectReq payload (184B):**
```
Offset  Len  Field
0x00    32   SSID (UTF-8, null-padded)
0x20    32   passphrase (UTF-8, null-padded)
0x40    32   reserved (zeros)
0x60     4   goAddr  = macOS IPv4 (we are TCP server)
0x64     4   staAddr = 0.0.0.0 (same-network: glasses use router DHCP)
0x68     4   subnetMask = 255.255.255.0
0x6C     4   dnsServer = 0x00000000
0x70     4   gateway   = 0x00000000
0x74     2   goChannel = WiFi MHz big-endian (e.g. 2437 for ch6)  ← MHz not channel!
0x76     2   acceptPortNum = TCP server port big-endian
0x78    64   PSK = PBKDF2-HMAC-SHA1(pass, ssid, 4096, 32) as 64-char hex string
```

**TCP model:** macOS = SERVER (accept), glasses = CLIENT (connect back).

**WiFi state machine:**
```
WifiTurnOnReq(0x92) → WifiStatusRes(0x91, state=3) → WifiConnectReq(0x94)
→ WifiConnectivityStatus(0x95, state=3) → TCP_ACCEPT
→ WifiDPSwitchPathReq(0x96, mode=1) → WifiDPSwitchPathRes(0x97)  → 30fps active
```

### 4.4 Other Commands

```
→ 0x30  OpenAppStartRequest   [0x30, 0x00, 0x00]
← 0x06  LevelNotification     touch sensor event
← 0xe5  LayoutEventNotify     suppress (flood)
→ 0xe9  DisplayTurnOn/Off     [0xe9, 0x00, 0x01, 0x01/0x00]
→ 0xc3  OpenAppMode           [0xc3, 0x00, 0x01, mode]
```

### 4.5 RX Name Map (for JSON events)

```
0x01 ACK              0x02 NAK               0x05 PING
0x06 LevelNotification 0x08 VersionResponse  0x0a ProtocolVersion
0x72 SettingsStatusResponse                  0x81 FotaStatus
0x91 WifiStatusRes    0x95 WifiConnectivityStatus
0x96 WifiDPSwitchPathReq                     0x97 WifiDPSwitchPathRes
0xe5 LayoutEventNotify 0xe8 ImageAck         0xff SyncResponse
```

---

## 5. JSON Event Schema

All events at `/tmp/glasses-events.jsonl` (one JSON per line).

| type | Fields |
|------|--------|
| `TX` | `ts, cmd, name, bytes, phase, wifi_active, ok` |
| `RX` | `ts, cmd, name, payload (hex), phase` |
| `STATE` | `ts, phase, wifi_phase, wifi_active, tcp_connected, bt_connected` |
| `LOG` | `ts, level (INFO/WARN/ERROR), msg` |
| `WIFI` | `ts, event (ENABLED/CONNECTED/SWITCHED/DROPPED), state` |
| `COMPRESS` | `ts, raw, compressed, ratio (0.0-1.0), ms` |

`ts` = Unix epoch in milliseconds (float).

**Example lines:**
```json
{"type":"TX","cmd":"0xe7","name":"LAYOUT all-white","bytes":5923,"phase":5,"wifi_active":false,"ok":true,"ts":1749200000000.0}
{"type":"RX","cmd":"0x91","name":"WifiStatusRes","payload":"03","phase":5,"ts":1749200001234.5}
{"type":"STATE","phase":5,"wifi_phase":11,"wifi_active":false,"tcp_connected":false,"bt_connected":true,"ts":1749200001300.0}
{"type":"WIFI","event":"ENABLED","state":3,"ts":1749200001301.0}
{"type":"COMPRESS","raw":57822,"compressed":137,"ratio":0.00237,"ms":1,"ts":1749200001500.0}
```

---

## 6. Test Framework

### Structure
```
tests/
  __init__.py
  conftest.py          pytest fixtures (proc, events, cmd)
  helpers/
    __init__.py
    events.py          EventStream (wait_for, all_since, clear)
  test_protocol.py     BT handshake tests
  test_wifi.py         WiFi flow tests (require same-network WiFi)
  test_display.py      Display/rendering tests
```

### Run tests
```bash
cd /Users/gerhardgustav/Desktop/hobby-dev/sony-sed-e1

# Install deps (first time)
uv sync

# All tests (requires glasses)
uv run pytest tests/ -v

# Protocol only (BT, no WiFi required)
uv run pytest tests/test_protocol.py -v

# Skip WiFi full-flow
uv run pytest tests/ -v -k "not full_flow"
```

### Key fixtures (conftest.py)

| Fixture | Scope | Description |
|---------|-------|-------------|
| `events` | session | `EventStream` at `/tmp/glasses-events.jsonl` |
| `proc` | function | Spawns `glasses-tool connect`, yields Popen, kills on teardown |
| `cmd(proc, s)` | helper fn | Writes `s\n` to proc stdin |

### Writing a new test
```python
from helpers.events import EventStream
from conftest import cmd

def test_my_feature(proc, events: EventStream):
    cmd(proc, "my_command")
    ev = events.wait_for(type="TX", cmd="0xXX", timeout=5)
    assert ev is not None
    assert ev["bytes"] == expected
```

---

## 7. tmux Session

```bash
./harness.tmux.sh          # 3-pane session
./harness.tmux.sh --test   # + auto-run pytest after 3s

# Pane layout:
#   Left:         glasses-tool TUI (node harness/dist/index.js)
#   Right top:    JSON event stream (tail -F /tmp/glasses-events.jsonl | python3 ...)
#   Right bottom: pytest runner
```

---

## 8. Config Files

| File | Keys |
|------|------|
| `macos-middleware/glasses.conf` | `bt_address=auto`, `rfcomm_channel=4` |
| `macos-middleware/.env` | `SSID=YourNetwork`, `PSWD=YourPassword` (same-network WiFi credentials) |
| `pyproject.toml` | Python env (`uv sync` installs pytest) |
| `/tmp/glasses-events.jsonl` | Live JSON event log (truncate to reset) |
| `/tmp/glasses_capture.log` | Raw BT hex dump |

---

## 9. Open Problems + WiFi Continuation

### Current status
| Step | Status |
|------|--------|
| BT connect + handshake | ✅ confirmed |
| Display (glider demo) | ✅ confirmed at ~2.5fps |
| `wifi on` → 0x91 ENABLED | ✅ confirmed |
| `wifi connect auto` (same-network) | ❌ needs hardware test with new arch |
| `wifi switch` → 30fps | ❌ not yet end-to-end tested |

### Next steps to test WiFi end-to-end

```bash
# 1. Connect Mac to WiFi network that glasses will also join

# 2. Put credentials in .env
cat > macos-middleware/.env << EOF
SSID=YourNetworkName
PSWD=YourPassword
EOF

# 3. Verify readiness
./glasses-wifi-setup.sh

# 4. Run and test WiFi flow
./macos-middleware/glasses-tool connect
# In REPL:
wifi on           # → wait for "ENABLED" (0x91 state=3)
wifi connect auto # → wait for "CONNECTED" (0x95 state=3) + "TCP connected"
wifi switch       # → wait for 0x97, 30fps glider starts
```

### Debugging
- **0x95 CONNECTED never arrives**: glasses may not be able to join the network. Try a mobile hotspot (phone), or verify SSID/PSWD in `.env` match exactly (case-sensitive).
- **TCP never connects after 0x95**: firewall blocking. Run: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate`
- **0x97 never arrives after wifi switch**: check `wifiClientFd >= 0` in STATE events (tcp_connected must be true before sending wifi switch).
- **Verify Mac IP**: `./glasses-wifi-setup.sh` checks en0 IP and .env consistency.

---

## 10. Repository Layout

```
sony-sed-e1/
├── macos-middleware/
│   ├── glasses-tool.swift     ← full protocol driver (Swift)
│   ├── glasses-tool           ← compiled binary
│   ├── glasses.conf           ← bt_address=auto, rfcomm_channel=4
│   └── glasses-wifi-setup.sh ← WiFi readiness checker (check en0 + .env)
├── harness/
│   ├── src/
│   │   ├── index.ts           ← TUI entry point
│   │   ├── events.ts          ← JSON event tailer
│   │   ├── state.ts           ← ProtocolState model
│   │   ├── process.ts         ← glasses-tool subprocess manager
│   │   └── components/        ← HeaderBar, EventLog, ProtocolState, etc.
│   ├── dist/                  ← compiled JS
│   └── package.json
├── tests/
│   ├── conftest.py
│   ├── helpers/events.py      ← EventStream.wait_for()
│   ├── test_protocol.py
│   ├── test_wifi.py
│   └── test_display.py
├── harness.tmux.sh            ← 3-pane tmux session
├── glasses-sdk/PROTOCOL_MAP.md
└── SKILL.md                   ← this file
```

## 11. Git Log Summary

```
3b95fbe fix: getInterfaceIP uses ipconfig subprocess, guard auto IP
b2b227e feat: multi-glasses, native DEFLATE, 30fps WiFi, stop/setup
e0dafbf refactor: glider on demand only
415d9ae feat: glider demo
a3fb5da feat: WiFi data path (TCP server, WifiConnectReq, state machine)
d3a36e8 feat: Game of Life rendering from macOS via BT SPP (first working display)
```

**Current state**: BT fully working. WiFi implemented (IP bug fixed). JSON logging added. TUI harness built. Needs WiFi end-to-end hardware test.
