# Sony SmartEyeglass SED-E1 — macOS toolkit

Drive Sony SED-E1 AR glasses from macOS via Bluetooth and WiFi. No Android required.

**Display**: 419×138 green monochrome. **Working**: BT connection, display rendering, glider animation. **In progress**: WiFi path (30fps).

---

## Prerequisites

- **macOS 12+** with Xcode Command Line Tools:
  ```bash
  xcode-select --install
  ```
- **Glasses paired via Bluetooth** — do this before running the tool:
  ```bash
  ./macos-middleware/glasses-tool pair   # prints step-by-step instructions
  ```
  Or manually: power on glasses (hold POWER switch 4+ sec), then pair in System Settings → Bluetooth.
- **Node.js 18+** — for the TUI harness (`node --version`)
- **Python 3.10+** and **uv** — for tests:
  ```bash
  # Install uv (if not present)
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

---

## Files

```
macos-middleware/glasses-tool.swift   single-file Swift CLI — the whole protocol driver
macos-middleware/glasses.conf.example config template
macos-middlewar./macos-middleware/glasses-wifi-setup.sh WiFi readiness checker
harness/                              TypeScript TUI — live event log, protocol state, shortcuts
tests/                                pytest hardware-in-the-loop tests
glasses-sdk/PROTOCOL_MAP.md          full reverse-engineered wire protocol
SKILL.md                              dense reference for agents and developers
CHANGELOG.md                          version history
```

---

## Build & run

```bash
cd macos-middleware
swiftc glasses-tool.swift -framework IOBluetooth -framework Foundation -o glasses-tool -O
cp glasses.conf.example glasses.conf   # edit if needed
./glasses-tool                         # scans for glasses, pick one, connects
```

Requires macOS + Xcode command-line tools. No other dependencies.

---

## TUI harness (recommended)

Live event log + protocol state panel with keyboard shortcuts:

```bash
cd harness && npm install && npm run build
node harness/dist/index.js             # auto-discovers glasses
node harness/dist/index.js --no-glasses  # dev mode (no hardware)
```

Or use the 3-pane tmux session (TUI + JSON stream + pytest runner):

```bash
./harness.tmux.sh
```

---

## REPL (after connect)

```
glider          start glider animation (~2.5 fps over BT)
stop            stop demo
wifi setup      step-by-step WiFi upgrade (30 fps)
wifi on         enable glasses WiFi radio
wifi connect auto   connect using .env credentials + auto IP
wifi switch     move display to WiFi — 30 fps glider starts
help            all commands
quit            disconnect
```

---

## WiFi (30fps)

Both Mac and glasses join the **same WiFi network**. No Internet Sharing or hotspot needed.

```bash
# 1. Put your WiFi credentials in macos-middleware/.env
cat > macos-middleware/.env << EOF
SSID=YourNetworkName
PSWD=YourPassword
EOF

# 2. Verify setup
./macos-middleware/glasses-wifi-setup.sh

# 3. In the REPL (after BT connect)
wifi on            # enable glasses WiFi radio
wifi connect auto  # glasses join your network, Mac opens TCP server
wifi switch        # move display path to WiFi → 30fps glider starts
```

---

## Tests

Requires glasses connected over BT.

```bash
uv sync           # install pytest
uv run pytest tests/test_protocol.py -v    # BT handshake (no WiFi needed)
uv run pytest tests/ -v                    # all tests
```

---

## Protocol essentials

See [`glasses-sdk/PROTOCOL_MAP.md`](glasses-sdk/PROTOCOL_MAP.md) for the full spec.

Key facts:
- Frame format: `[cmdId:1B][len:2B big-endian][payload]`
- **SyncResponse (0xFF) after FotaStatus is mandatory** — without it glasses ignore all commands
- **LayoutInit x/y are scroll offsets** not dimensions — use `(0, 0, state=0)`
- Display command: `0xe7` LayoutPlaceRemoveCommand with 8-bit grayscale + raw DEFLATE (`wbits=-15`)
- WiFi: macOS is TCP server, glasses connect back after receiving `WifiConnectReq (0x94)`
