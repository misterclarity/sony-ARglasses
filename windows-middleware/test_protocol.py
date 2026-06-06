"""Offline protocol unit tests for the Windows driver. No hardware needed.

Run:  python -m pytest test_protocol.py -v
  or: python test_protocol.py
"""
import io
import os
import zlib
from contextlib import redirect_stdout

import glasses_tool as g


def _silent_session():
    jlog = g.JSONEventLog(os.devnull)
    return g.Session(g.MockTransport(), g.Config(), jlog)


def test_deflate_raw_roundtrips():
    img = g.pattern_checker()
    comp = g.deflate_raw(img)
    assert bytes(zlib.decompress(comp, -15)) == bytes(img)
    assert len(comp) < len(img)  # checker compresses well


def test_layout_cmd_header():
    cmd = g.build_layout_cmd(g.pattern_white())
    assert cmd[0] == 0xe7
    assert (cmd[1] << 8) | cmd[2] == len(cmd) - 3


def test_layout_subcommand_dimensions():
    # PLACE_IMGOBJ encodes width=419 (0x01a3) and height=138 (0x008a)
    cmd = g.build_layout_cmd(g.pattern_black())
    assert b"\x01\xa3\x00\x8a" in cmd


def test_psk_is_64_hex_chars():
    psk = g.derive_psk("Net", "password")
    assert len(psk) == 64
    int(psk, 16)  # valid hex


def test_wifi_connect_req_size_and_layout():
    psk = g.derive_psk("Net", "password")
    req = g.build_wifi_connect_req("Net", "password", psk, "192.168.137.1", 50000, 2437)
    assert len(req) == 187  # 3 header + 184 payload (HANDOFF spec)
    assert req[0:3] == bytes([0x94, 0x00, 0xB8])
    off = 3
    assert (req[off + 0x76] << 8) | req[off + 0x77] == 50000     # acceptPortNum
    assert (req[off + 0x74] << 8) | req[off + 0x75] == 2437      # goChannel MHz
    assert req[off + 0x60:off + 0x64] == bytes([192, 168, 137, 1])  # goAddr
    assert req[off + 0x64:off + 0x68] == bytes([192, 168, 137, 2])  # staAddr (last octet flipped)


def test_frame_parser_reassembles_fragmented_stream():
    got = []
    fp = g.FrameParser(lambda c, p: got.append((c, p)))
    stream = (bytes([0x0a, 0x00, 0x02, 0x01, 0x00])
              + bytes([0x72, 0x00, 0x01, 0x00])
              + bytes([0x81, 0x00, 0x00]))
    for i in range(0, len(stream), 3):       # awkward 3-byte fragments
        fp.feed(stream[i:i + 3])
    assert [c for c, _ in got] == [0x0a, 0x72, 0x81]
    assert got[0][1] == bytes([0x01, 0x00])  # ProtocolVersion payload preserved


def test_handshake_drives_to_phase5():
    s = _silent_session()
    # The exact glasses->host sequence that should walk init_phase 0 -> 5.
    frames = [
        bytes([0x0a, 0x00, 0x02, 0x01, 0x00]),  # ProtocolVersion
        bytes([0x72, 0x00, 0x01, 0x00]),         # SettingsStatusResponse
        bytes([0x08, 0x00, 0x03]) + b"1.0",      # VersionResponse
        bytes([0x81, 0x00, 0x01, 0x00]),         # FotaStatus
        bytes([0x06, 0x00, 0x01, 0x03]),         # LevelNotification -> ready
    ]
    with redirect_stdout(io.StringIO()):         # swallow the ready banner
        for fr in frames:
            s.parser.feed(fr)
    assert s.init_phase == 5
    s.shutdown()


def test_wifi_status_enabled_advances_phase():
    s = _silent_session()
    s.wifi_phase = 10                            # as if 'wifi on' was sent
    with redirect_stdout(io.StringIO()):
        s.parser.feed(bytes([0x91, 0x00, 0x01, 0x03]))  # WifiStatusRes ENABLED
    assert s.wifi_phase == 11
    s.shutdown()


def test_hex_to_bytes():
    assert g.hex_to_bytes("e9 00 01 01") == bytes([0xe9, 0x00, 0x01, 0x01])
    assert g.hex_to_bytes("0xe900") == bytes([0xe9, 0x00])
    assert g.hex_to_bytes("abc") is None  # odd length


if __name__ == "__main__":
    import sys
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
