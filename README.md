# Sony SmartEyeglass SED-E1 — macOS + Windows toolkit

Drive Sony SED-E1 AR glasses from macOS or Windows via Bluetooth and WiFi. No Android required.

**Display**: 419×138 green monochrome. **Working**: BT connection, display rendering, glider animation, and the **30fps WiFi path** — both confirmed on real hardware from Windows (the WiFi handoff had never been verified on any platform before).

The protocol is identical on both platforms; only the Bluetooth transport differs. On macOS it uses `IOBluetooth` RFCOMM. On Windows the glasses don't advertise standard SPP (UUID `0x1101`) — their data service is a **custom UUID on RFCOMM channel 4**, so Windows never makes a COM port for them. The Python tool instead opens a raw **`AF_BTH` RFCOMM socket** straight to the device MAC + channel via `ctypes`/Winsock — no third-party packages, pure standard library. **Confirmed working on real hardware** (handshake + animated display).

---

## Files

```
macos-middleware/glasses-tool.swift     single-file Swift CLI (macOS, IOBluetooth)
macos-middleware/glasses.conf.example   macOS config template
macos-middleware/glasses-wifi-setup.sh  macOS hotspot setup (sudo, optional)
windows-middleware/glasses_tool.py      single-file Python CLI (Windows, ctypes AF_BTH, stdlib only)
windows-middleware/dashboard.py         tkinter GUI: one-button connect, mic record/play, live log
windows-middleware/requirements-dashboard.txt  optional GUI extras (winsdk, sounddevice, numpy)
windows-middleware/glasses.conf.example Windows config template
windows-middleware/test_protocol.py     offline protocol unit tests (no hardware)
windows-middleware/requirements.txt     (empty — no third-party deps)
glasses-sdk/PROTOCOL_MAP.md             full reverse-engineered wire protocol
```

---

## Build & run — macOS

```bash
cd macos-middleware
swiftc glasses-tool.swift -framework IOBluetooth -framework Foundation -o glasses-tool -O
cp glasses.conf.example glasses.conf   # edit if needed
./glasses-tool                         # scans for glasses, pick one, connects
```

Requires macOS + Xcode command-line tools. No other dependencies.

---

## Build & run — Windows

No build, no `pip install` — it's pure Python standard library. Just **pair the glasses once** in Settings → Bluetooth, then:

```powershell
cd windows-middleware
copy glasses.conf.example glasses.conf   # optional; edit if needed
python glasses_tool.py --demo            # discover, connect, run the glider
```

Requires Python 3.9+ on Windows. `requirements.txt` is intentionally empty.

```powershell
python glasses_tool.py scan                       # list paired SmartEyeglass + MAC
python glasses_tool.py                            # connect + interactive REPL
python glasses_tool.py connect ac:9b:0a:37:a6:64  # connect to a specific MAC
python glasses_tool.py connect --channel 4        # override RFCOMM channel (default 4)
python glasses_tool.py --hold 20                  # connect, watch handshake 20s, no REPL
python glasses_tool.py --no-glasses               # REPL + engine on a mock transport
python test_protocol.py                           # offline protocol tests (no hardware)
```

**Connecting (important):**
- Each connection consumes the glasses' **"Waiting to connect"** state. Power-cycle them fresh (hold POWER ~4s) before each attempt — if you get `WSA 10060` (timeout), they weren't ready.
- On reaching display-ready, the glasses show an **alignment screen** — press the controller button once to confirm. The display then activates (with `--demo`, the glider starts automatically).
- If Windows keeps auto-grabbing the glasses' Hands-Free ("mic") profile and they won't enter "Waiting to connect", disable it: Control Panel → Devices and Printers → SmartEyeglass → Properties → Services → untick **Handsfree Telephony**.

The tool writes a structured JSON event stream to `glasses-events.jsonl` (override with `GLASSES_EVENTS`) for tooling/tests.

---

## WiFi (30fps) — Windows dashboard

The WiFi path runs the display over TCP at ~30fps. Windows hosts the network (Mobile Hotspot) and the glasses join it and connect back. **Confirmed working on real hardware** via the dashboard.

```powershell
cd windows-middleware
pip install -r requirements-dashboard.txt   # winsdk + sounddevice + numpy (GUI only)
python dashboard.py
```

The GUI still opens without those extras, but the **one-button connect** (auto hotspot toggle) and **mic record/play** need them.

**Single Connect button** — does the whole flow on one press:

1. Turns the Mobile Hotspot **off** (a fresh BT link can't form while it runs).
2. Connects **Bluetooth** + handshake (retries until the glasses appear). **Confirm the alignment screen on the glasses** (controller button) — the one unavoidable physical step.
3. Turns the Mobile Hotspot **on** (2.4 GHz, using the SSID/password from your Windows hotspot config; mirror them in `glasses.conf` as `wifi_ssid`/`wifi_pswd`).
4. Runs the WiFi handoff (`0x91 ENABLED → 0x94 → 0x95 CONNECTED → glasses TCP connect → 0x97 WIFI`) → 30fps glider over WiFi.

**Single-radio caveat (why the ordering matters):** most laptops have one combined Wi-Fi/Bluetooth chip that **cannot** run a 2.4 GHz hotspot while *establishing* a Bluetooth link — the hotspot starves BT. Bringing BT up first (hotspot off) and only then enabling the hotspot lets the already-open link survive the handoff. The Connect button sequences this automatically. (A second radio — e.g. a USB Wi-Fi adapter for the hotspot — avoids the issue entirely.)

**Microphone:** the glasses expose a standard Bluetooth Hands-Free mic. **Record mic (5s)** captures from it to `glasses-mic.wav`; **Play back** plays it on the PC speakers. The headless equivalent of the WiFi flow is `python glasses_tool.py connect --wifi-demo`.

---

## Dashboard features (camera, voice, sensors, images)

These were reverse-engineered from the Sony SmartEyeglass host APK (camera/sensor command bytes) and wired into `dashboard.py`. They need the optional extras in `requirements-dashboard.txt` (winsdk, sounddevice, numpy, Pillow, vosk + the Vosk model):

| Button | What it does | Protocol |
|---|---|---|
| **Show Image…** | Pick a PNG/JPG/etc; auto-grayscale + fit to 419×138 → display | `0xE7` LayoutPlaceRemove |
| **Take Photo** | Capture a still JPEG → `glasses-photo.jpg` + preview | `0xCE` mode → `0x38` sensor-start(19) → `0xB4` capture → `0xB5/0xB6/0xB7` |
| **Voice** | Offline keyword spotting (Vosk) on the glasses mic — say "photo" to capture | mic via HFP; 8k→16k upsample |
| **Coordinates** | Live accelerometer/gyro/magnetometer/light rendered on the AR display + console | `0x38` SensorStart(id) → data `0x3A`/`0xBC`/`0xBD`/`0x3B` |
| **Help** | Prints a full capability reference (every sensor, command byte, units) to the console | — |

Camera command IDs: `CameraMode 0xCE`, `CaptureReq 0xB4`, `CaptureResp 0xB5`, `CaptureData 0xB6`, `DataDone 0xB7`, `DataAck 0xF1`. Sensor IDs: accelerometer 1, rotation-vector 12, gyro 13, magnetometer 14, light 16, camera 19. The glasses answer each `SensorStart` with a short burst, so the Coordinates view re-arms stale sensors to keep the readout live.

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

WiFi credentials: create `macos-middleware/.env`:
```
SSID=YourNetwork
PSWD=YourPassword
```

---

## Protocol essentials

See [`glasses-sdk/PROTOCOL_MAP.md`](glasses-sdk/PROTOCOL_MAP.md) for the full spec.

Key facts:
- Frame format: `[cmdId:1B][len:2B big-endian][payload]`
- **SyncResponse (0xFF) after FotaStatus is mandatory** — without it glasses ignore all commands
- **LayoutInit x/y are scroll offsets** not dimensions — use `(0, 0, state=0)`
- Display command: `0xe7` LayoutPlaceRemoveCommand with 8-bit grayscale + raw DEFLATE (`wbits=-15`)
- WiFi: macOS is TCP server, glasses TCP-connect back after receiving `WifiConnectReq (0x94)`
