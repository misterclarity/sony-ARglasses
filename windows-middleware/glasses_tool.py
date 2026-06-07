#!/usr/bin/env python3
"""
glasses_tool.py
Sony SED-E1 Windows direct connection tool - BT (SPP/COM) + WiFi

Windows port of macos-middleware/glasses-tool.swift. The protocol is identical;
only the Bluetooth transport differs. The SED-E1 does NOT advertise standard
SPP (UUID 0x1101) - its data service is a custom UUID on RFCOMM channel 4 - so
Windows never creates a COM port for it. We instead open a raw AF_BTH RFCOMM
socket straight to the device MAC + channel (via ctypes/Winsock), exactly like
IOBluetooth opens a channel by number on macOS. No third-party packages: the
whole tool is Python standard library. Everything else - the handshake state
machine, DEFLATE display frames, WiFi TCP path - is a straight port.

Modes:
  (default)       discover paired glasses, connect, REPL (ready banner)
  scan            list paired SmartEyeglass devices + MAC
  connect [ADDR]  connect to a MAC (e.g. aa:bb:cc:dd:ee:ff), REPL
  --demo          after the handshake, auto-run the glider (good first test)
  --hold N        connect, watch the handshake for N seconds, no REPL
  --channel N     override the RFCOMM channel (default 4)
  --no-glasses    run the REPL + engine with a mock transport (no hardware)

Run (no install step needed):
  python glasses_tool.py            # discover + connect
  python glasses_tool.py --demo     # connect + glider; tap the glasses when
                                     # they show the alignment screen

Note: each connection consumes the glasses' "Waiting to connect" state, so
power-cycle them fresh before each attempt. On reaching display-ready the glasses
show an alignment screen - press the controller button once to confirm.

Requires: Python 3.9+ on Windows. Stdlib only (ctypes/zlib/hashlib/socket).
"""

import sys
import os
import time
import json
import zlib
import socket
import hashlib
import threading
import subprocess
import random
import struct
import ctypes
from ctypes import wintypes
from datetime import datetime

# Motion-sensor IDs (from the SmartEyeglass host APK, Mckinley*SensorConfig)
SENSOR_ACCEL = 1
SENSOR_ROTATION = 12
SENSOR_GYRO = 13
SENSOR_MAG = 14
SENSOR_LIGHT = 16

try:
    import winreg
except ImportError:
    winreg = None

# Display geometry (green monochrome panel)
W = 419
H = 138

# ANSI colours
CLR_RED = "\033[31m"
CLR_GRN = "\033[32m"
CLR_YLW = "\033[33m"
CLR_BLU = "\033[34m"
CLR_MAG = "\033[35m"
CLR_CYN = "\033[36m"
CLR_RST = "\033[0m"

if os.name == "nt":
    # Enable ANSI escape processing on Windows 10+ consoles.
    os.system("")


# Optional log sinks (e.g. the GUI dashboard) receive plain "[ts] msg" lines
# plus the color name, so they can render without ANSI codes.
_log_sinks = []


def add_log_sink(fn):
    _log_sinks.append(fn)


def log(msg, color=CLR_RST):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{CLR_RST}", flush=True)
    if _log_sinks:
        line = f"[{ts}] {msg}"
        for sink in _log_sinks:
            try:
                sink(line, color)
            except Exception:
                pass


# ----------------------------------------------------------------------------
# JSON event log (additive; mirrors HANDOFF.md schema, consumed by TUI/tests)
# ----------------------------------------------------------------------------
class JSONEventLog:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._fh = open(path, "w", encoding="utf-8")
        except OSError as exc:
            log(f"JSON log disabled ({exc})", CLR_YLW)
            self._fh = None

    def emit(self, etype, **fields):
        if not self._fh:
            return
        rec = {"ts": round(time.time() * 1000, 1), "type": etype}
        rec.update(fields)
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()


# RX command name map (for events + logging)
RX_NAMES = {
    0x01: "ACK", 0x02: "NAK", 0x05: "PING", 0x06: "LevelNotification",
    0x08: "VersionResponse", 0x0a: "ProtocolVersion",
    0x31: "OpenAppStartResponse", 0x32: "Touch", 0x36: "ImageAck",
    0x3c: "KeyEvent", 0x72: "SettingsStatusResponse", 0x81: "FotaStatus",
    0x91: "WifiStatusRes", 0x95: "WifiConnectivityStatus",
    0x96: "WifiDPSwitchPathReq", 0x97: "WifiDPSwitchPathRes",
    0xb5: "CameraCaptureResponse", 0xb6: "CameraCaptureData",
    0xb7: "CameraCaptureDataDone",
    0x3a: "Acceleration", 0xbc: "Gyro", 0xbd: "Magnetometer",
    0x3b: "LightSensor", 0x3e: "BatterySensor",
    0xe5: "LayoutEventNotify", 0xe8: "ImageAck", 0xff: "SyncResponse",
}


# ----------------------------------------------------------------------------
# Hex helpers
# ----------------------------------------------------------------------------
def hex_to_bytes(s):
    clean = s.replace(" ", "").replace("0x", "")
    if len(clean) % 2 != 0:
        return None
    try:
        return bytes.fromhex(clean)
    except ValueError:
        return None


def hexdump(data):
    out = []
    for row in range(0, len(data), 16):
        chunk = data[row:row + 16]
        h = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        a = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
        out.append(f"  {row:04x}  {h} |{a}|")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Config (.conf + .env), mirrors GlassesConfig.load() in the Swift tool
# ----------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.bt_address = "auto"      # "auto" = discover paired SmartEyeglass, or a MAC
        self.rfcomm_channel = 4       # SED-E1 "AHA" data service is RFCOMM channel 4
        self.bt_name = "SmartEyeglass"  # name substring for auto-discovery
        self.capture_log = ""
        self.wifi_ssid = ""
        self.wifi_pswd = ""

    @classmethod
    def load(cls):
        cfg = cls()
        here = os.path.dirname(os.path.abspath(__file__))
        cwd = os.getcwd()

        for path in [os.path.join(here, "glasses.conf"),
                     os.path.join(cwd, "glasses.conf"), "glasses.conf"]:
            if not os.path.isfile(path):
                continue
            log(f"Config: {path}", CLR_CYN)
            for line in _read_lines(path):
                k, v = _kv(line)
                if k is None:
                    continue
                if k == "bt_address":
                    cfg.bt_address = v
                elif k == "rfcomm_channel":
                    try:
                        cfg.rfcomm_channel = int(v)
                    except ValueError:
                        pass
                elif k == "bt_name":
                    cfg.bt_name = v
                elif k == "capture_log":
                    cfg.capture_log = v
                elif k == "wifi_ssid":
                    cfg.wifi_ssid = v
                elif k == "wifi_pswd":
                    cfg.wifi_pswd = v
            break

        for path in [os.path.join(here, ".env"),
                     os.path.join(cwd, ".env"), ".env"]:
            if not os.path.isfile(path):
                continue
            for line in _read_lines(path):
                k, v = _kv(line)
                if k == "SSID" and not cfg.wifi_ssid:
                    cfg.wifi_ssid = v
                elif k == "PSWD" and not cfg.wifi_pswd:
                    cfg.wifi_pswd = v
            break

        return cfg


def _read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _kv(line):
    t = line.strip()
    if not t or t.startswith("#") or "=" not in t:
        return None, None
    k, v = t.split("=", 1)
    return k.strip(), v.strip()


# ----------------------------------------------------------------------------
# Network helpers (Windows equivalents of the Swift ipconfig/airport calls)
# ----------------------------------------------------------------------------
def get_hotspot_ip():
    """Find the Windows Mobile Hotspot / ICS adapter IPv4 (192.168.137.1 by
    default). The glasses must TCP-connect to *this* address, not the host's
    main LAN IP, so it takes priority over get_local_ip() for WiFi."""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        if "IPv4" in line and "192.168.137." in line:
            return line.split(":", 1)[1].strip()
    return None


def get_local_ip():
    """Best-effort primary IPv4 of this host (the hotspot adapter usually wins
    only if it is the default route; override with an explicit IP otherwise)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def detect_wifi_channel_mhz():
    """Parse `netsh wlan show interfaces` for the current channel -> MHz."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        low = line.lower()
        if "channel" in low and ":" in line:
            tok = line.split(":", 1)[1].strip().split()[0].split(",")[0]
            try:
                ch = int(tok)
            except ValueError:
                continue
            if 1 <= ch <= 14:
                mhz = 2407 + ch * 5
                log(f"Detected WiFi channel {ch} = {mhz} MHz", CLR_CYN)
                return mhz
    log("Using default channel 6 (2437 MHz)", CLR_YLW)
    return 2437


# ----------------------------------------------------------------------------
# Paired-device (MAC) discovery
#
# The SED-E1 does NOT advertise standard SPP (UUID 0x1101); its data service is
# a custom UUID on RFCOMM channel 4. Windows therefore never creates a COM port
# for it. We instead open a raw AF_BTH RFCOMM socket directly to the device MAC
# + channel, exactly like IOBluetooth opens a channel by number on macOS.
# ----------------------------------------------------------------------------
_BT_DEVICES_KEY = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"


def find_paired_glasses(name_substr="SmartEyeglass"):
    """Return [(mac_hex, name), ...] for paired BT devices whose name matches.
    Reads the BTHPORT registry (fast, no admin needed for read on most setups)."""
    out = []
    if winreg is None:
        return out
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BT_DEVICES_KEY)
    except OSError:
        return out
    i = 0
    while True:
        try:
            mac = winreg.EnumKey(root, i)
            i += 1
        except OSError:
            break
        try:
            with winreg.OpenKey(root, mac) as dk:
                raw, _ = winreg.QueryValueEx(dk, "Name")
                name = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "ignore")
        except OSError:
            continue
        if name_substr.lower() in name.lower():
            out.append((mac.lower(), name))
    return out


def normalize_mac(s):
    """'aa:bb:cc:dd:ee:ff' / 'AC-9B-..' / 'aabbccddeeff' -> 'aabbccddeeff'."""
    h = "".join(c for c in s.lower() if c in "0123456789abcdef")
    return h if len(h) == 12 else None


def resolve_address(cfg, explicit=None):
    """Return a 12-hex-digit MAC string, or None."""
    raw = explicit if explicit is not None else cfg.bt_address
    if raw and raw.lower() != "auto":
        mac = normalize_mac(raw)
        if not mac:
            log(f"Invalid bt_address: {raw}", CLR_RED)
        return mac

    log(f"Discovering paired '{cfg.bt_name}'...", CLR_CYN)
    found = find_paired_glasses(cfg.bt_name)
    if not found:
        log(f"No paired '{cfg.bt_name}' found.", CLR_RED)
        log("  -> Pair the glasses in Windows Settings > Bluetooth first.", CLR_YLW)
        log("  -> Or pass an address: python glasses_tool.py connect aa:bb:cc:dd:ee:ff", CLR_YLW)
        return None
    if len(found) == 1:
        mac, name = found[0]
        log(f"  Found {name} [{mac}]", CLR_GRN)
        return mac
    log(f"Found {len(found)} matching devices:", CLR_CYN)
    for idx, (mac, name) in enumerate(found):
        log(f"  [{idx + 1}] {name} [{mac}]", CLR_CYN)
    print(f"{CLR_YLW}Select [1-{len(found)}] (Enter = 1): {CLR_RST}", end="", flush=True)
    try:
        line = input().strip()
    except EOFError:
        line = ""
    if line.isdigit() and 1 <= int(line) <= len(found):
        return found[int(line) - 1][0]
    return found[0][0]


# ----------------------------------------------------------------------------
# Winsock AF_BTH RFCOMM bindings (ctypes; no third-party dependency)
# ----------------------------------------------------------------------------
_AF_BTH = 32
_SOCK_STREAM = 1
_BTHPROTO_RFCOMM = 3
_SOL_SOCKET = 0xFFFF
_SO_RCVTIMEO = 0x1006
_INVALID_SOCKET = (1 << 64) - 1
_SOCKET_ERROR = -1
_WSAETIMEDOUT = 10060

_ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
_ws2.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_ws2.socket.restype = ctypes.c_uint64
_ws2.connect.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
_ws2.connect.restype = ctypes.c_int
_ws2.send.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_ws2.send.restype = ctypes.c_int
_ws2.recv.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_ws2.recv.restype = ctypes.c_int
_ws2.setsockopt.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int,
                            ctypes.c_void_p, ctypes.c_int]
_ws2.setsockopt.restype = ctypes.c_int
_ws2.closesocket.argtypes = [ctypes.c_uint64]
_ws2.closesocket.restype = ctypes.c_int

_wsa_started = False


def _wsa_startup():
    global _wsa_started
    if not _wsa_started:
        data = ctypes.create_string_buffer(512)
        _ws2.WSAStartup(0x0202, data)
        _wsa_started = True


class _SOCKADDR_BTH(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_uint64),
        ("serviceClassId", ctypes.c_byte * 16),
        ("port", ctypes.c_uint32),
    ]


# ----------------------------------------------------------------------------
# Transports: a transport just moves bytes. The protocol layer is unaware of
# whether it is talking over Bluetooth RFCOMM, WiFi TCP, or a mock pipe.
# ----------------------------------------------------------------------------
class BTRFCOMMTransport:
    name = "BT"

    def __init__(self, mac_hex, channel, connect_timeout_note=True):
        _wsa_startup()
        self.port = f"{mac_hex}@ch{channel}"
        self._sock = _ws2.socket(_AF_BTH, _SOCK_STREAM, _BTHPROTO_RFCOMM)
        if self._sock == _INVALID_SOCKET:
            raise OSError(f"socket() failed (WSA {ctypes.get_last_error()})")

        addr = _SOCKADDR_BTH()
        addr.addressFamily = _AF_BTH
        addr.btAddr = int(mac_hex, 16)
        addr.port = channel
        rc = _ws2.connect(self._sock, ctypes.byref(addr), ctypes.sizeof(addr))
        if rc == _SOCKET_ERROR:
            err = ctypes.get_last_error()
            _ws2.closesocket(self._sock)
            hint = ""
            if err == _WSAETIMEDOUT:
                hint = " (glasses not in 'Waiting to connect'? power-cycle them)"
            raise OSError(f"RFCOMM connect failed (WSA {err}){hint}")

        # 200ms receive timeout so the read loop can poll for shutdown.
        tv = ctypes.c_uint32(200)
        _ws2.setsockopt(self._sock, _SOL_SOCKET, _SO_RCVTIMEO,
                        ctypes.byref(tv), ctypes.sizeof(tv))
        self._buf = ctypes.create_string_buffer(8192)

    def send(self, data):
        data = bytes(data)
        sent = 0
        while sent < len(data):
            n = _ws2.send(self._sock, data[sent:], len(data) - sent, 0)
            if n == _SOCKET_ERROR:
                raise OSError(f"send failed (WSA {ctypes.get_last_error()})")
            sent += n

    def recv(self):
        n = _ws2.recv(self._sock, self._buf, len(self._buf), 0)
        if n == _SOCKET_ERROR:
            err = ctypes.get_last_error()
            if err == _WSAETIMEDOUT:
                return b""        # no data this tick; keep looping
            raise OSError(f"recv failed (WSA {err})")
        if n == 0:
            raise OSError("connection closed by glasses")
        return self._buf.raw[:n]

    def close(self):
        try:
            _ws2.closesocket(self._sock)
        except Exception:
            pass


class MockTransport:
    """No hardware. Echoes nothing but lets the engine/REPL run end-to-end and
    feeds a synthetic handshake so the state machine reaches phase 5."""
    name = "MOCK"

    def __init__(self):
        self._inbox = bytearray()
        self._lock = threading.Lock()
        self.port = "MOCK"
        # Queue a scripted handshake the read loop will deliver over time.
        self._script = [
            (0.3, bytes([0x0a, 0x00, 0x02, 0x01, 0x00])),         # ProtocolVersion
            (0.5, bytes([0x72, 0x00, 0x01, 0x00])),               # SettingsStatusResponse
            (0.7, bytes([0x08, 0x00, 0x03]) + b"1.0"),            # VersionResponse
            (0.9, bytes([0x81, 0x00, 0x01, 0x00])),               # FotaStatus
            (1.3, bytes([0x06, 0x00, 0x01, 0x03])),               # LevelNotification
        ]
        self._t0 = time.time()

    def send(self, data):
        pass

    def recv(self):
        time.sleep(0.05)
        due = time.time() - self._t0
        out = bytearray()
        remaining = []
        for delay, payload in self._script:
            if due >= delay:
                out += payload
            else:
                remaining.append((delay, payload))
        self._script = remaining
        return bytes(out)

    def close(self):
        pass


# ----------------------------------------------------------------------------
# Frame reassembler: protocol frames are [cmd:1][len:2 BE][payload:len].
# Serial/TCP reads fragment arbitrarily, so we buffer and emit whole frames.
# ----------------------------------------------------------------------------
class FrameParser:
    def __init__(self, on_frame):
        self._buf = bytearray()
        self._on_frame = on_frame

    def feed(self, data):
        self._buf += data
        while len(self._buf) >= 3:
            length = (self._buf[1] << 8) | self._buf[2]
            total = 3 + length
            if len(self._buf) < total:
                break
            cmd = self._buf[0]
            payload = bytes(self._buf[3:total])
            del self._buf[:total]
            self._on_frame(cmd, payload)


# ----------------------------------------------------------------------------
# Display: DEFLATE + LayoutPlaceRemoveCommand builder + Game of Life engine
# ----------------------------------------------------------------------------
def deflate_raw(data):
    """Raw DEFLATE, wbits=-15 (Java Deflater nowrap=true equivalent)."""
    co = zlib.compressobj(zlib.Z_BEST_COMPRESSION, zlib.DEFLATED, -15)
    return co.compress(bytes(data)) + co.flush()


def build_layout_cmd(grayscale, jlog=None):
    """0xe7 LayoutPlaceRemoveCommand with PLACE_STATE/PLACE_IMGOBJ/PLACE_IMGDATA."""
    t0 = time.time()
    compressed = deflate_raw(grayscale)
    if jlog:
        ratio = len(compressed) / max(len(grayscale), 1)
        jlog.emit("COMPRESS", raw=len(grayscale), compressed=len(compressed),
                  ratio=round(ratio, 4), ms=round((time.time() - t0) * 1000, 1))

    sub1 = bytes([0x01, 0x00, 0x0a]) + bytes(10)  # PLACE_STATE

    sub2 = bytearray([0x03, 0x00, 0x18])          # PLACE_IMGOBJ, len=24
    sub2 += bytes([0x00, 0x00, 0x00, 0x00])       # objId, layerId, pad
    sub2 += bytes([0x00, 0x00, 0x00, 0x00])       # x=0
    sub2 += bytes([0x00, 0x00, 0x00, 0x00])       # y=0
    sub2 += bytes([0x01, 0xa3])                   # width=419
    sub2 += bytes([0x00, 0x8a])                   # height=138
    sub2 += bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

    img_len = 2 + 1 + len(compressed)
    sub3 = bytearray([0x07, (img_len >> 8) & 0xff, img_len & 0xff])
    sub3 += bytes([0x00, 0x00])                   # objId=0
    sub3 += bytes([0x01])                         # imgFormat=1 (8-bit mono + DEFLATE)
    sub3 += compressed

    payload = bytes(sub1) + bytes(sub2) + bytes(sub3)
    total = len(payload)
    return bytes([0xe7, (total >> 8) & 0xff, total & 0xff]) + payload


class GameOfLife:
    def __init__(self):
        self.grid = [[False] * W for _ in range(H)]
        self.generation = 0

    def reset(self):
        self.grid = [[False] * W for _ in range(H)]
        self.generation = 0

    def step(self):
        g = self.grid
        nxt = [[False] * W for _ in range(H)]
        for y in range(H):
            row = nxt[y]
            for x in range(W):
                n = 0
                for dy in (-1, 0, 1):
                    gy = g[(y + dy) % H]
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        if gy[(x + dx) % W]:
                            n += 1
                row[x] = (n == 2 or n == 3) if g[y][x] else (n == 3)
        self.grid = nxt
        self.generation += 1

    def to_image(self):
        img = bytearray(W * H)
        for y in range(H):
            base = y * W
            row = self.grid[y]
            for x in range(W):
                if row[x]:
                    img[base + x] = 255
        return img


GLIDER_SHAPES = [
    [(1, 0), (2, 1), (0, 2), (1, 2), (2, 2)],  # SE
    [(1, 0), (0, 1), (0, 2), (1, 2), (2, 2)],  # SW
    [(1, 2), (2, 1), (0, 0), (1, 0), (2, 0)],  # NE
    [(1, 2), (0, 1), (0, 0), (1, 0), (2, 0)],  # NW
]


# ----------------------------------------------------------------------------
# Test patterns (parity with the Swift REPL)
# ----------------------------------------------------------------------------
def pattern_white():
    return bytearray([0xff]) * (W * H)


def pattern_black():
    return bytearray(W * H)


def pattern_checker():
    img = bytearray(W * H)
    for y in range(H):
        for x in range(W):
            if (x // 16 + y // 16) % 2 == 0:
                img[y * W + x] = 255
    return img


def pattern_stripes():
    img = bytearray(W * H)
    for y in range(H):
        if y % 8 < 4:
            for x in range(W):
                img[y * W + x] = 255
    return img


def pattern_cross():
    img = bytearray(W * H)
    cx, cy = W // 2, H // 2
    for x in range(W):
        img[cy * W + x] = 255
        img[x] = 255
        img[(H - 1) * W + x] = 255
    for y in range(H):
        img[y * W + cx] = 255
        img[y * W] = 255
        img[y * W + W - 1] = 255
    return img


# ----------------------------------------------------------------------------
# WiFi: macOS-as-server model from the Swift tool. We open a TCP server (OS
# picks the port), tell the glasses our SSID/PSK/port via WifiConnectReq (0x94),
# and the glasses connect back. PSK = PBKDF2-HMAC-SHA1(pass, ssid, 4096, 32).
# ----------------------------------------------------------------------------
def derive_psk(ssid, passphrase):
    return hashlib.pbkdf2_hmac(
        "sha1", passphrase.encode("utf-8"), ssid.encode("utf-8"), 4096, 32
    ).hex()


def build_wifi_connect_req(ssid, passphrase, psk, go_ip, port, channel_mhz):
    payload = bytearray(184)
    payload[0x00:0x00 + len(ssid.encode()[:32])] = ssid.encode("utf-8")[:32]
    pb = passphrase.encode("utf-8")[:32]
    payload[0x20:0x20 + len(pb)] = pb

    octs = [int(o) for o in go_ip.split(".") if o.isdigit()]
    if len(octs) != 4:
        log(f"Invalid IP: {go_ip}", CLR_RED)
        return b""
    payload[0x60:0x64] = bytes(octs)                       # goAddr
    sta = list(octs)
    sta[3] = 2 if octs[3] == 1 else 1
    payload[0x64:0x68] = bytes(sta)                        # staAddr
    payload[0x68:0x6c] = bytes([255, 255, 255, 0])         # subnet
    payload[0x74] = (channel_mhz >> 8) & 0xff
    payload[0x75] = channel_mhz & 0xff
    payload[0x76] = (port >> 8) & 0xff
    payload[0x77] = port & 0xff
    pk = psk.encode("utf-8")[:64]
    payload[0x78:0x78 + len(pk)] = pk

    return bytes([0x94, 0x00, 0xB8]) + bytes(payload)


# ----------------------------------------------------------------------------
# Session: owns the transport, protocol state machine, and the display loop.
# ----------------------------------------------------------------------------
class Session:
    def __init__(self, transport, cfg, jlog):
        self.tp = transport
        self.cfg = cfg
        self.jlog = jlog
        self.parser = FrameParser(self.on_frame)

        self.init_phase = 0     # 0..5 BT handshake (see Swift comments)
        self.rx_count = 0
        self.running = True
        self.auto_glider = False  # start the glider automatically on reaching phase 5
        self.auto_wifi = False    # run the full wifi on->connect->switch sequence

        # Camera capture state
        self._cam = None          # active capture: dict(packets, size, status, id, on_done)
        self._cam_lock = threading.Lock()

        # Latest motion-sensor values: name -> (x, y, z), plus last-update time + count
        self.sensors = {}
        self.sensor_ts = {}
        self.sensor_count = 0

        # Display loop
        self.gol = GameOfLife()
        self._disp_thread = None
        self._disp_stop = threading.Event()
        self.frames_sent = 0

        # WiFi state
        self.wifi_phase = 0
        self.wifi_active = False
        self.wifi_ssid = cfg.wifi_ssid or "DIRECT-ma-SonyGlasses"
        self.wifi_pass = cfg.wifi_pswd or "SonyGlass2024!"
        self.wifi_go_ip = "192.168.137.1"   # Windows Mobile Hotspot default
        self.wifi_port = 0
        self._wifi_server = None
        self._wifi_client = None
        self._wifi_lock = threading.Lock()

    # -- emit STATE snapshot -------------------------------------------------
    def emit_state(self):
        self.jlog.emit("STATE", phase=self.init_phase, wifi_phase=self.wifi_phase,
                       wifi_active=self.wifi_active,
                       tcp_connected=self._wifi_client is not None,
                       bt_connected=self.tp.name in ("BT", "MOCK"))

    # -- TX ------------------------------------------------------------------
    def send_cmd(self, data, label):
        cmd = data[0]
        if self.wifi_active and self._wifi_client is not None:
            self._send_tcp(data, label)
            return
        try:
            self.tp.send(bytes(data))
            ok = True
        except Exception as exc:
            log(f"TX failed ({exc})", CLR_RED)
            ok = False
        preview = " ".join(f"{b:02x}" for b in data[:12])
        log(f"-> {self.tp.name} TX {label} {len(data)}B {'OK' if ok else 'FAIL'}: {preview}...", CLR_BLU)
        self.jlog.emit("TX", cmd=f"0x{cmd:02x}", name=label, bytes=len(data),
                       phase=self.init_phase, wifi_active=self.wifi_active, ok=ok)

    def _send_tcp(self, data, label):
        with self._wifi_lock:
            cli = self._wifi_client
            if cli is None:
                self.send_cmd(data, label + "(BT-fallback)")
                return
            try:
                cli.sendall(bytes(data))
                ok = True
            except OSError:
                ok = False
        log(f"-> WiFi TX {label} {len(data)}B {'OK' if ok else 'FAIL'}", CLR_BLU)
        self.jlog.emit("TX", cmd=f"0x{data[0]:02x}", name=label, bytes=len(data),
                       phase=self.init_phase, wifi_active=True, ok=ok)

    # -- read loop -----------------------------------------------------------
    def read_loop(self):
        while self.running:
            try:
                data = self.tp.recv()
            except Exception as exc:
                log(f"Read error: {exc}", CLR_RED)
                break
            if data:
                self.rx_count += len(data)
                self.parser.feed(data)
        self.running = False

    # -- RX dispatch (port of the Swift onData state machine) ----------------
    def on_frame(self, cmd, payload):
        name = RX_NAMES.get(cmd, f"0x{cmd:02x}")
        self.jlog.emit("RX", cmd=f"0x{cmd:02x}", name=name,
                       payload=payload[:8].hex(), phase=self.init_phase)
        p = self.init_phase
        b0 = payload[0] if payload else 0

        # WiFi responses can arrive in any phase
        if cmd == 0x91:
            self._on_wifi_status(b0)
            return
        if cmd == 0x95:
            self._on_wifi_connectivity(b0)
            return
        if cmd == 0x97:
            self._on_wifi_switch(b0)
            return
        if cmd == 0x96:
            log(f"WifiDPSwitchPathReq(0x96) from glasses: path={b0}", CLR_YLW)
            if b0 == 0:
                self.wifi_active = False
                self.wifi_phase = 0
            return

        # Motion-sensor data (accuracy:4, timestamp:4, x,y,z float32 BE)
        if cmd == 0x3A:
            self._on_sensor("accel", payload)
            return
        if cmd == 0xBC:
            self._on_sensor("gyro", payload)
            return
        if cmd == 0xBD:
            self._on_sensor("mag", payload)
            return
        if cmd == 0x3B:
            self._on_light(payload)
            return

        # Camera capture responses (any phase)
        if cmd == 0xB5:
            self._on_camera_response(payload)
            return
        if cmd == 0xB6:
            self._on_camera_data(payload)
            return
        if cmd == 0xB7:
            self._on_camera_done(payload)
            return

        # BT handshake
        if p == 0 and cmd == 0x0a:
            self.init_phase = 1
            log("P1: ProtocolVersion -> SettingsStatusRequest", CLR_MAG)
            self.send_cmd([0x71, 0x00, 0x00], "SettingsStatusRequest")
        elif p == 1 and cmd == 0x72:
            self.init_phase = 2
            log("P2: SettingsStatusResponse -> VersionRequest", CLR_MAG)
            self.send_cmd([0x07, 0x00, 0x01, 0x01], "VersionRequest")
        elif p == 2 and cmd == 0x08:
            self.init_phase = 3
            ver = payload.decode("ascii", "replace") if payload else "?"
            log(f"P3: FW={ver} -> NewHostApp", CLR_MAG)
            self.send_cmd([0x85, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00], "NewHostApp(0)")
            t = threading.Timer(5.0, self._fota_timeout)
            t.daemon = True
            t.start()
        elif p == 3 and cmd == 0x81:
            log("P3: FotaStatus -> SyncResponse (critical!)", CLR_MAG)
            self.send_cmd([0xff, 0x00, 0x00], "SyncResponse")
            self.init_phase = 4
            log("Tap the touch sensor OR type 'help' for commands.", CLR_YLW)
        elif p == 4 and cmd == 0x31:
            log("P4->5: OpenAppStartResponse! Glasses confirmed.", CLR_GRN)
            self._enter_ready(send_start=False)
        elif p == 4 and cmd == 0x06:
            log(f"P4->5: LevelNotification(level={b0})! Glasses READY.", CLR_GRN)
            self._enter_ready(send_start=True)
        elif p == 4 and cmd == 0x81:
            log("P4: FotaStatus (ignoring, waiting for LevelNotification)", CLR_YLW)
        elif cmd == 0x0a and p > 0:
            self.init_phase = 1
            log("Re-received ProtocolVersion. Restarting handshake.", CLR_MAG)
            self.send_cmd([0x71, 0x00, 0x00], "SettingsStatusRequest")
        elif p == 5:
            if cmd not in (0xe5,):
                log(f"   [{name}]", CLR_CYN)
        else:
            log(f"   [phase={p} cmd=0x{cmd:02x}]", CLR_YLW)
        self.emit_state()

    def _fota_timeout(self):
        if self.init_phase != 3:
            return
        log("No FotaStatus after 5s - advancing.", CLR_YLW)
        self.init_phase = 4
        self.send_cmd([0x30, 0x00, 0x00], "OpenAppStartRequest")
        log("TAP the glasses touch sensor to confirm!", CLR_YLW)

    def _enter_ready(self, send_start):
        self.init_phase = 5
        if send_start:
            self.send_cmd([0x30, 0x00, 0x00], "OpenAppStartRequest")
        self.send_cmd([0xe0, 0x00, 0x0a] + [0] * 10, "LayoutInit(0,0,state=0)")
        time.sleep(0.5)
        self.send_cmd(build_layout_cmd(pattern_white(), self.jlog), "LAYOUT all-white")
        self.jlog.emit("WIFI", event="READY", state=5)
        if self.auto_wifi:
            log("Phase 5 reached - auto-starting WiFi sequence (wifi on)...", CLR_GRN)
            self.send_cmd([0x92, 0x00, 0x00], "WifiTurnOnReq")
            self.wifi_phase = 10
        elif self.auto_glider:
            log("Phase 5 reached - auto-starting glider demo.", CLR_GRN)
            self.start_glider(fps=2.5)
        else:
            print_ready_banner()

    def auto_wifi_connect(self):
        """Resolve creds + hotspot IP and start the WiFi connect handshake."""
        ssid = self.cfg.wifi_ssid or self.wifi_ssid
        passwd = self.cfg.wifi_pswd or self.wifi_pass
        ip = get_hotspot_ip() or self.wifi_go_ip
        if not ssid or not passwd:
            log("auto-wifi: no SSID/PSWD in glasses.conf or .env - cannot connect.", CLR_RED)
            return
        log(f"auto-wifi: connecting glasses to '{ssid}' (host {ip})", CLR_CYN)
        self.wifi_start_connect(ssid, passwd, ip)

    # -- WiFi RX handlers ----------------------------------------------------
    def _on_wifi_status(self, status):
        names = ["DISABLING", "DISABLED", "ENABLING", "ENABLED", "UNKNOWN"]
        sn = names[status] if status < len(names) else "?"
        log(f"WifiStatusRes(0x91): {sn} ({status})",
            CLR_GRN if status == 3 else CLR_MAG)
        if status == 3 and self.wifi_phase == 10:
            self.wifi_phase = 11
            self.jlog.emit("WIFI", event="ENABLED", state=status)
            if self.auto_wifi:
                self.auto_wifi_connect()
            else:
                log("Glasses WiFi ENABLED. Type 'wifi connect auto'.", CLR_GRN)

    def _on_wifi_connectivity(self, status):
        names = ["DISCONNECTING", "DISCONNECTED", "CONNECTING", "CONNECTED", "UNKNOWN"]
        sn = names[status] if status < len(names) else "?"
        log(f"WifiConnectivityStatus(0x95): {sn} ({status})",
            CLR_GRN if status == 3 else CLR_MAG)
        if status == 3:
            log(f"Glasses joined WiFi! Waiting for TCP on port {self.wifi_port}...", CLR_GRN)
            self.jlog.emit("WIFI", event="CONNECTED", state=status)

    def _on_wifi_switch(self, path):
        log(f"WifiDPSwitchPathRes(0x97): path={'WIFI' if path == 1 else 'BT'}", CLR_GRN)
        if path == 1:
            self.wifi_active = True
            self.wifi_phase = 13
            self.jlog.emit("WIFI", event="SWITCHED", state=1)
            log("WiFi data path ACTIVE - 30fps glider.", CLR_GRN)
            self.start_glider(fps=30)
        else:
            self.wifi_active = False
            self.wifi_phase = 0

    # -- WiFi TCP server -----------------------------------------------------
    def wifi_create_server(self):
        if self._wifi_server:
            try:
                self._wifi_server.close()
            except OSError:
                pass
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", 0))
        srv.listen(1)
        self.wifi_port = srv.getsockname()[1]
        self._wifi_server = srv
        log(f"WiFi TCP server listening on port {self.wifi_port}", CLR_GRN)
        return self.wifi_port

    def wifi_start_accept(self):
        srv = self._wifi_server

        def _accept():
            log(f"Waiting for glasses TCP connection on port {self.wifi_port}...", CLR_YLW)
            try:
                cli, addr = srv.accept()
            except OSError:
                return
            with self._wifi_lock:
                self._wifi_client = cli
            self.wifi_phase = 12
            if self.auto_wifi:
                log(f"Glasses TCP connected from {addr}! Auto-switching path to WiFi...", CLR_GRN)
                self.send_cmd([0x96, 0x00, 0x01, 0x01], "WifiDPSwitchPathReq(WIFI)")
            else:
                log(f"Glasses TCP connected from {addr}! Type 'wifi switch'.", CLR_GRN)
            while self.running:
                try:
                    data = cli.recv(8192)
                except OSError:
                    break
                if not data:
                    break
                self.parser.feed(data)
            with self._wifi_lock:
                self._wifi_client = None
            if self.wifi_active:
                self.wifi_active = False
                self.wifi_phase = 0
                log("WiFi dropped - BT active again.", CLR_YLW)

        threading.Thread(target=_accept, daemon=True).start()

    def wifi_start_connect(self, ssid, passphrase, go_ip):
        port = self.wifi_create_server()
        if not port:
            return
        self.wifi_start_accept()
        ch = detect_wifi_channel_mhz()
        log(f"Deriving PSK for '{ssid}'...", CLR_CYN)
        psk = derive_psk(ssid, passphrase)
        req = build_wifi_connect_req(ssid, passphrase, psk, go_ip, port, ch)
        if not req:
            return
        self.send_cmd(req, "WifiConnectReq(0x94)")
        self.wifi_phase = 11
        log("WifiConnectReq sent. Watch for 0x95 CONNECTED then TCP accept.", CLR_CYN)
        log(f"  SSID={ssid}  IP={go_ip}  port={port}  ch={ch}MHz", CLR_YLW)

    # -- camera capture (protocol from the SmartEyeglass host APK) -----------
    # JPEG quality: 1=STANDARD 2=FINE 3=SUPERFINE
    # Resolution: 0=3M 1=SXGA 2=XGA 3=SVGA 4=VGA 5=HVGA 6=QVGA 7=QQVGA
    def capture_photo(self, quality=2, resolution=4, on_done=None):
        """Take a still photo. on_done(jpeg_bytes_or_None, info) is called when
        the transfer finishes. Smaller resolutions transfer faster over BT."""
        if self.init_phase != 5:
            log("Camera: not ready (need phase 5).", CLR_RED)
            if on_done:
                on_done(None, {"error": "not ready"})
            return
        with self._cam_lock:
            self._cam = {"packets": {}, "size": 0, "status": None,
                         "id": None, "on_done": on_done, "t0": time.time(),
                         "quality": quality, "resolution": resolution}

        def _sequence():
            # Mirrors the host app's still-capture flow:
            #   cameraModeSet -> startCamera(STILL)=SensorStart(19,6,0) -> cameraCapture
            # OpenAppCameraMode(0xCE): int32 BE = (fps<<12)|(res<<8)|(quality<<4)|mode
            val = ((0 & 3) << 12) | ((resolution & 7) << 8) | ((quality & 3) << 4) | 0
            self.send_cmd([0xCE, 0x00, 0x04] + list(val.to_bytes(4, "big")),
                          f"OpenAppCameraMode(q={quality},res={resolution},still)")
            time.sleep(0.2)
            # OpenAppSensorStart(0x38): sensorId=19(camera), rate=6, interval=0
            self.send_cmd([0x38, 0x00, 0x04, 0x13, 0x06, 0x00, 0x00],
                          "OpenAppSensorStart(camera=19)")
            time.sleep(0.7)   # let the camera warm up
            self.send_cmd([0xB4, 0x00, 0x00], "OpenAppCameraCaptureRequest")
            log("Camera: capture requested - waiting for image...", CLR_CYN)

        threading.Thread(target=_sequence, daemon=True).start()

    def _stop_camera_sensor(self):
        self.send_cmd([0x39, 0x00, 0x01, 0x13], "OpenAppSensorStop(camera=19)")

    # -- motion sensors ------------------------------------------------------
    # rate: 1=FASTEST 2=GAME 3=NORMAL 4=UI 5=INTERRUPT (6=user interval)
    def start_sensor(self, sensor_id, rate=3):
        self.send_cmd([0x38, 0x00, 0x02, sensor_id & 0xFF, rate & 0xFF],
                      f"OpenAppSensorStart(id={sensor_id},rate={rate})")

    def stop_sensor(self, sensor_id):
        self.send_cmd([0x39, 0x00, 0x01, sensor_id & 0xFF],
                      f"OpenAppSensorStop(id={sensor_id})")

    def _on_sensor(self, name, payload):
        if len(payload) < 20:
            return
        nfloats = (len(payload) - 8) // 4
        vals = struct.unpack(">" + "f" * nfloats, payload[8:8 + nfloats * 4])
        self.sensors[name] = vals[:3]
        self.sensor_ts[name] = time.time()
        self.sensor_count += 1
        self.jlog.emit("SENSOR", name=name, x=round(vals[0], 3),
                       y=round(vals[1], 3), z=round(vals[2], 3))

    def _on_light(self, payload):
        # LightSensor: accuracy(4), timestamp(4), lightValue(int32 BE)
        if len(payload) < 12:
            return
        lux = int.from_bytes(payload[8:12], "big", signed=True)
        self.sensors["light"] = (lux,)
        self.sensor_ts["light"] = time.time()
        self.sensor_count += 1
        self.jlog.emit("SENSOR", name="light", lux=lux)

    def _on_camera_response(self, payload):
        if len(payload) < 6:
            return
        status = payload[0]
        image_id = payload[1]
        size = int.from_bytes(payload[2:6], "big")
        log(f"Camera: CaptureResponse status={status} id={image_id} size={size}B",
            CLR_GRN if status == 0 else CLR_RED)
        self.jlog.emit("CAMERA", event="RESPONSE", status=status, id=image_id, size=size)
        with self._cam_lock:
            if self._cam is not None:
                self._cam["status"] = status
                self._cam["size"] = size
                self._cam["id"] = image_id
        if status != 0:
            self._finish_camera(ok=False, reason=f"status {status}")

    def _on_camera_data(self, payload):
        if len(payload) < 3:
            return
        pkt = int.from_bytes(payload[1:3], "big")
        chunk = payload[3:]
        with self._cam_lock:
            if self._cam is not None:
                self._cam["packets"][pkt] = chunk

    def _on_camera_done(self, payload):
        image_id = payload[0] if payload else 0
        reason = payload[1] if len(payload) > 1 else 0
        # Acknowledge receipt (mirrors the host app: single Ack after Done).
        self.send_cmd([0xF1, 0x00, 0x01, image_id & 0xFF], "CameraCaptureDataAck")
        log(f"Camera: DataDone id={image_id} reason={reason}", CLR_GRN)
        self._finish_camera(ok=(reason == 0), reason=f"reason {reason}")

    def _finish_camera(self, ok, reason=""):
        with self._cam_lock:
            cam = self._cam
            self._cam = None
        if cam is None:
            return
        self._stop_camera_sensor()   # stop the camera sensor (mirrors stopCamera)
        jpeg = b"".join(cam["packets"][k] for k in sorted(cam["packets"]))
        on_done = cam["on_done"]
        valid = jpeg[:2] == b"\xff\xd8"
        if ok and valid:
            dt = time.time() - cam["t0"]
            log(f"Camera: got JPEG {len(jpeg)}B in {dt:.1f}s "
                f"(expected {cam['size']}B).", CLR_GRN)
            self.jlog.emit("CAMERA", event="DONE", bytes=len(jpeg))
            if on_done:
                on_done(jpeg, {"size": cam["size"], "received": len(jpeg)})
        else:
            log(f"Camera: capture failed ({reason}); got {len(jpeg)}B, "
                f"valid_jpeg={valid}", CLR_RED)
            if on_done:
                on_done(None, {"error": reason, "received": len(jpeg)})

    # -- display loop --------------------------------------------------------
    def _spawn_glider(self):
        shape = random.choice(GLIDER_SHAPES)
        ox = random.randint(5, W - 11)
        oy = random.randint(5, H - 11)
        for dx, dy in shape:
            x, y = ox + dx, oy + dy
            if 0 <= x < W and 0 <= y < H:
                self.gol.grid[y][x] = True

    def start_glider(self, fps=2.5):
        self.stop_display()
        self.gol.reset()
        self._spawn_glider()
        self._disp_stop.clear()
        interval = 1.0 / fps
        spawn_every = random.uniform(4, 10)

        def _loop():
            elapsed = 0.0
            nxt_spawn = spawn_every
            self._send_frame()
            while not self._disp_stop.is_set():
                time.sleep(interval)
                self.gol.step()
                self.gol.step()
                elapsed += 2 * interval
                if elapsed >= nxt_spawn:
                    self._spawn_glider()
                    elapsed = 0.0
                    nxt_spawn = random.uniform(4, 10)
                self._send_frame()

        self._disp_thread = threading.Thread(target=_loop, daemon=True)
        self._disp_thread.start()
        log(f"Glider running at ~{fps:g}fps. Type 'stop' to stop.", CLR_MAG)

    def _send_frame(self):
        cmd = build_layout_cmd(self.gol.to_image(), self.jlog)
        self.frames_sent += 1
        self.send_cmd(cmd, f"GOL gen={self.gol.generation}")

    def stop_display(self):
        self._disp_stop.set()
        if self._disp_thread and self._disp_thread.is_alive():
            self._disp_thread.join(timeout=1.0)
        self._disp_thread = None

    # -- shutdown ------------------------------------------------------------
    def shutdown(self):
        self.running = False
        self.stop_display()
        if self._wifi_server:
            try:
                self._wifi_server.close()
            except OSError:
                pass
        self.tp.close()


# ----------------------------------------------------------------------------
# REPL
# ----------------------------------------------------------------------------
def print_ready_banner():
    print(f"""{CLR_GRN}
================================================
  CONNECTED - glasses ready
================================================{CLR_RST}
  {CLR_YLW}glider{CLR_RST}        start glider demo (~2.5fps BT)
  {CLR_YLW}stop{CLR_RST}          stop demo
  {CLR_YLW}white/black/checker/stripes/cross{CLR_RST}  test patterns
  {CLR_YLW}wifi on{CLR_RST}       enable glasses WiFi radio
  {CLR_YLW}wifi connect auto{CLR_RST}   connect using .env credentials
  {CLR_YLW}wifi switch{CLR_RST}   activate WiFi path (30fps)
  {CLR_YLW}help / quit{CLR_RST}""")
    print(f"{CLR_MAG}> {CLR_RST}", end="", flush=True)


def print_help():
    print(f"""{CLR_CYN}
=== COMMANDS ==={CLR_RST}
  {CLR_GRN}Display:{CLR_RST} glider  stop  white  black  checker  stripes  cross
          on  off  linit  clear
  {CLR_GRN}WiFi:{CLR_RST}    wifi on | off | status | connect auto | connect <ssid> <pass> <ip>
          wifi switch | wifi bt | wifi setup
  {CLR_GRN}Other:{CLR_RST}   raw <hex>   help   quit""")
    print(f"{CLR_MAG}> {CLR_RST}", end="", flush=True)


PATTERNS = {
    "white": pattern_white, "black": pattern_black, "checker": pattern_checker,
    "stripes": pattern_stripes, "cross": pattern_cross,
}


def repl(session):
    cfg = session.cfg
    while session.running:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        parts = line.lstrip("﻿").strip().split()
        if not parts:
            print(f"{CLR_MAG}> {CLR_RST}", end="", flush=True)
            continue
        cmd = parts[0].lower()

        if cmd in PATTERNS:
            session.send_cmd(build_layout_cmd(PATTERNS[cmd](), session.jlog),
                             f"LAYOUT {cmd}")
        elif cmd == "glider":
            session.start_glider(fps=2.5)
        elif cmd == "stop":
            session.stop_display()
            session.send_cmd(build_layout_cmd(pattern_black(), session.jlog),
                             "STOP - black frame")
            log("Demo stopped. Type 'glider' to restart.", CLR_YLW)
        elif cmd == "on":
            session.send_cmd([0xe9, 0x00, 0x01, 0x01], "DisplayTurnOn")
        elif cmd == "off":
            session.send_cmd([0xe9, 0x00, 0x01, 0x00], "DisplayTurnOff")
        elif cmd == "linit":
            session.send_cmd([0xe0, 0x00, 0x0a] + [0] * 10, "LayoutInit")
        elif cmd == "clear":
            session.send_cmd([0xb1, 0x00, 0x00], "OpenAppClearScreen")
        elif cmd == "wifi":
            _repl_wifi(session, cfg, parts)
        elif cmd == "raw":
            data = hex_to_bytes("".join(parts[1:]))
            if data:
                session.send_cmd(data, f"RAW {len(data)}B")
            else:
                log("Invalid hex. Usage: raw e9 00 01 01", CLR_RED)
        elif cmd in ("help", "h", "?"):
            print_help()
            continue
        elif cmd in ("quit", "exit", "q"):
            log("Disconnecting...", CLR_YLW)
            session.shutdown()
            break
        else:
            log(f"Unknown command: {cmd}. Type 'help'.", CLR_RED)
        print(f"{CLR_MAG}> {CLR_RST}", end="", flush=True)


def _repl_wifi(session, cfg, parts):
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "on":
        session.send_cmd([0x92, 0x00, 0x00], "WifiTurnOnReq")
        session.wifi_phase = 10
        log("Sent WifiTurnOnReq (0x92). Waiting for 0x91 ENABLED...", CLR_YLW)
    elif sub == "off":
        session.send_cmd([0x93, 0x00, 0x00], "WifiTurnOffReq")
        session.wifi_phase = 0
        session.wifi_active = False
    elif sub == "status":
        session.send_cmd([0x90, 0x00, 0x00], "WifiStatusReq")
    elif sub == "connect":
        if len(parts) > 2 and parts[2] == "auto":
            ssid = cfg.wifi_ssid or session.wifi_ssid
            passwd = cfg.wifi_pswd or session.wifi_pass
            ip = get_hotspot_ip() or session.wifi_go_ip
            if not ssid or not passwd:
                log("No SSID/PSWD. Use: wifi connect <ssid> <pass> <ip>", CLR_RED)
                return
            log(f"Auto: SSID={ssid}  IP={ip}", CLR_CYN)
            session.wifi_start_connect(ssid, passwd, ip)
        else:
            ssid = parts[2] if len(parts) > 2 else session.wifi_ssid
            passwd = parts[3] if len(parts) > 3 else session.wifi_pass
            ip = parts[4] if len(parts) > 4 else session.wifi_go_ip
            session.wifi_start_connect(ssid, passwd, ip)
    elif sub == "switch":
        if session._wifi_client is None:
            log("No TCP connection yet. Wait for glasses to connect.", CLR_YLW)
        else:
            session.send_cmd([0x96, 0x00, 0x01, 0x01], "WifiDPSwitchPathReq(WIFI)")
            log("Sent path switch. Waiting for 0x97...", CLR_YLW)
    elif sub == "bt":
        session.send_cmd([0x96, 0x00, 0x01, 0x00], "WifiDPSwitchPathReq(BT)")
        session.wifi_active = False
        session.wifi_phase = 0
    elif sub == "setup":
        print_wifi_setup(session)
    else:
        print_wifi_setup(session)


def print_wifi_setup(session):
    ip = get_local_ip() or session.wifi_go_ip
    print(f"""{CLR_CYN}
=== WiFi SETUP (Windows) ===
Windows = TCP server; glasses connect back over WiFi Direct.{CLR_RST}
1. Enable Mobile Hotspot (Settings > Network > Mobile hotspot),
   or share an adapter. Host IP is usually 192.168.137.1.
2. {CLR_GRN}wifi on{CLR_RST}            -> wait for 0x91 ENABLED
3. {CLR_GRN}wifi connect auto{CLR_RST}  -> wait for 0x95 CONNECTED + TCP accept
   (or: wifi connect <ssid> <pass> {ip})
4. {CLR_GRN}wifi switch{CLR_RST}        -> 0x97 WIFI, 30fps glider starts
""")
    print(f"{CLR_MAG}> {CLR_RST}", end="", flush=True)


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------
def cmd_scan(cfg):
    found = find_paired_glasses(cfg.bt_name)
    if not found:
        log(f"No paired '{cfg.bt_name}' found. Pair the glasses first "
            "(Settings > Bluetooth).", CLR_RED)
        return
    log(f"Paired '{cfg.bt_name}' devices ({len(found)}):", CLR_GRN)
    for mac, name in found:
        pretty = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
        log(f"  {name}  [{pretty}]  (channel {cfg.rfcomm_channel})", CLR_CYN)


def run_session(transport, cfg, jlog, hold=0, auto_glider=False, auto_wifi=False):
    session = Session(transport, cfg, jlog)
    session.auto_glider = auto_glider
    session.auto_wifi = auto_wifi
    reader = threading.Thread(target=session.read_loop, daemon=True)
    reader.start()
    log(f"Connected via {transport.name} ({transport.port}). Handshake starting...", CLR_CYN)
    if transport.name == "MOCK":
        log("MOCK mode: synthetic handshake, no real glasses.", CLR_YLW)
    try:
        if hold > 0:
            log(f"Holding connection {hold}s (no REPL); watching handshake...", CLR_YLW)
            deadline = time.time() + hold
            while session.running and time.time() < deadline:
                time.sleep(0.2)
            log(f"Hold elapsed. Final phase={session.init_phase}.", CLR_CYN)
        else:
            repl(session)
    finally:
        session.shutdown()


def main():
    args = sys.argv[1:]
    cfg = Config.load()
    jlog_path = os.environ.get("GLASSES_EVENTS",
                               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "glasses-events.jsonl"))
    jlog = JSONEventLog(jlog_path)

    no_glasses = "--no-glasses" in args
    args = [a for a in args if a != "--no-glasses"]

    demo = "--demo" in args
    args = [a for a in args if a != "--demo"]

    wifi_demo = "--wifi-demo" in args
    args = [a for a in args if a != "--wifi-demo"]

    def take_int_opt(flag, default):
        """Pull a '--flag value' pair out of args; return the int value."""
        val = default
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                try:
                    val = int(args[i + 1])
                except ValueError:
                    pass
                del args[i:i + 2]
            else:
                del args[i]
        return val

    hold = take_int_opt("--hold", 0)
    channel = take_int_opt("--channel", cfg.rfcomm_channel)
    retry_seconds = take_int_opt("--retry", 40)

    if args and args[0] == "scan":
        cmd_scan(cfg)
        return
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return

    if no_glasses:
        run_session(MockTransport(), cfg, jlog)
        return

    if args and args[0] == "connect":
        explicit = args[1] if len(args) > 1 else None
    else:
        explicit = args[0] if args else None

    mac = resolve_address(cfg, explicit)
    if not mac:
        sys.exit(1)
    pretty = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
    log(f"Connecting to {pretty} on RFCOMM channel {channel} "
        f"(retrying up to {retry_seconds}s - power-cycle glasses to 'Waiting to connect' now)...",
        CLR_CYN)
    transport = None
    deadline = time.time() + retry_seconds
    attempt = 0
    while transport is None:
        attempt += 1
        try:
            transport = BTRFCOMMTransport(mac, channel)
        except Exception as exc:
            if time.time() >= deadline:
                log(f"Gave up after {attempt} attempts: {exc}", CLR_RED)
                log("  -> Make sure the glasses show 'Waiting to connect' and retry.", CLR_YLW)
                sys.exit(1)
            log(f"  attempt {attempt} failed ({exc}); retrying...", CLR_YLW)
            time.sleep(1.0)
    if (demo or wifi_demo) and hold == 0:
        hold = 60 if wifi_demo else 45
    run_session(transport, cfg, jlog, hold=hold, auto_glider=demo,
                auto_wifi=wifi_demo)


if __name__ == "__main__":
    main()
