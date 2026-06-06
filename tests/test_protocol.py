"""
Protocol handshake tests — require glasses connected over BT.
"""
import time
import pytest
from helpers.events import EventStream
from conftest import cmd


def test_reaches_phase5(proc, events: EventStream):
    """Full BT handshake must reach phase 5 (display_ready)."""
    ev = events.wait_for(type="STATE", timeout=30)
    assert ev is not None, "No STATE event received within 30s"
    # Wait until phase 5
    deadline = time.time() + 30
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=2)
        if ev and ev.get("phase") == 5:
            break
    assert ev is not None and ev.get("phase") == 5, f"Expected phase=5, got {ev}"


def test_sync_response_after_fota(proc, events: EventStream):
    """SyncResponse (0xff TX) must be sent after FotaStatus (0x81 RX)."""
    fota = events.wait_for(type="RX", cmd="0x81", timeout=20)
    assert fota is not None, "FotaStatus (0x81) not received within 20s"
    sync = events.wait_for(type="TX", cmd="0xff", timeout=5)
    assert sync is not None, "SyncResponse (0xff TX) not sent within 5s of FotaStatus"


def test_protocol_version_is_first_rx(proc, events: EventStream):
    """ProtocolVersion (0x0a) must be the first command from glasses."""
    ev = events.wait_for(type="RX", timeout=25)
    assert ev is not None, "No RX event in first 15s"
    assert ev.get("cmd") == "0x0a", (
        f"Expected ProtocolVersion (0x0a) first, got {ev.get('cmd')} ({ev.get('name')})"
    )


def test_settings_status_req_sent(proc, events: EventStream):
    """SettingsStatusRequest (0x71) must be sent after ProtocolVersion."""
    events.wait_for(type="RX", cmd="0x0a", timeout=25)
    ev = events.wait_for(type="TX", cmd="0x71", timeout=3)
    assert ev is not None, "SettingsStatusRequest (0x71) not sent"


def test_compression_ratio(proc, events: EventStream):
    """Start glider and verify compression ratio < 10%."""
    # Wait until ready
    deadline = time.time() + 30
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=2)
        if ev and ev.get("phase") == 5:
            break
    cmd(proc, "glider")
    c = events.wait_for(type="COMPRESS", timeout=8)
    assert c is not None, "No COMPRESS event within 8s of starting glider"
    assert c["ratio"] < 0.1, f"Compression ratio too high: {c['ratio']:.3f} (expected < 0.1)"


def test_bt_connected_state(proc, events: EventStream):
    """After connecting, STATE must report bt_connected=True."""
    ev = events.wait_for(type="STATE", timeout=15)
    assert ev is not None, "No STATE event received"
    # Wait for a STATE that shows bt_connected
    deadline = time.time() + 20
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=2)
        if ev and ev.get("bt_connected"):
            break
    assert ev is not None and ev.get("bt_connected"), "bt_connected never True"
