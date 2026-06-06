"""
WiFi tests — require Mac and glasses on the same WiFi network.
Put credentials in macos-middleware/.env and run ./glasses-wifi-setup.sh first.
Run after BT handshake is confirmed working.
"""
import time
import pytest
from helpers.events import EventStream
from conftest import cmd


def _wait_phase5(events: EventStream, timeout: float = 30.0):
    """Block until protocol reaches phase 5."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=2)
        if ev and ev.get("phase") == 5:
            return True
    return False


def test_wifi_on_enabled(proc, events: EventStream):
    """wifi on → 0x91 WifiStatusRes with state=3 (ENABLED)."""
    assert _wait_phase5(events), "Did not reach phase5 before wifi test"
    cmd(proc, "wifi on")
    ev = events.wait_for(type="WIFI", event="ENABLED", timeout=8)
    assert ev is not None, "WIFI ENABLED event not received within 8s"
    assert ev.get("state") == 3, f"Expected state=3 (ENABLED), got {ev.get('state')}"


def test_wifi_on_triggers_rx91(proc, events: EventStream):
    """wifi on → 0x91 RX command must arrive."""
    assert _wait_phase5(events), "Did not reach phase5"
    cmd(proc, "wifi on")
    ev = events.wait_for(type="RX", cmd="0x91", timeout=8)
    assert ev is not None, "WifiStatusRes (0x91) not received"


def test_wifi_connect_req_size(proc, events: EventStream):
    """WifiConnectReq (0x94) payload must be exactly 187 bytes (3 hdr + 184)."""
    assert _wait_phase5(events), "Did not reach phase5"
    cmd(proc, "wifi on")
    events.wait_for(type="WIFI", event="ENABLED", timeout=8)
    cmd(proc, "wifi connect auto")
    ev = events.wait_for(type="TX", cmd="0x94", timeout=10)
    assert ev is not None, "WifiConnectReq (0x94) TX event not received"
    assert ev["bytes"] == 187, f"Expected 187 bytes, got {ev['bytes']}"


def test_wifi_state_transitions(proc, events: EventStream):
    """wifi on → wifiPhase=11 in STATE event."""
    assert _wait_phase5(events), "Did not reach phase5"
    cmd(proc, "wifi on")
    ev = events.wait_for(type="STATE", timeout=8)
    deadline = time.time() + 8
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=1)
        if ev and ev.get("wifi_phase") == 11:
            break
    assert ev is not None and ev.get("wifi_phase") == 11, (
        f"Expected wifi_phase=11 after wifi on, got {ev}"
    )


def test_wifi_full_flow(proc, events: EventStream):
    """Full WiFi flow: on → connect → TCP accept → switch.
    Requires Mac and glasses on same WiFi network; credentials in macos-middleware/.env.
    """
    assert _wait_phase5(events), "Did not reach phase5"

    cmd(proc, "wifi on")
    ev = events.wait_for(type="WIFI", event="ENABLED", timeout=8)
    assert ev is not None, "WiFi not ENABLED"

    cmd(proc, "wifi connect auto")
    ev = events.wait_for(type="WIFI", event="CONNECTED", timeout=20)
    assert ev is not None, (
        "Glasses did not join WiFi (CONNECTED event not seen). "
        "Check: are Mac and glasses on the same network? SSID/PSWD in .env correct? "
        "Run: ./glasses-wifi-setup.sh"
    )

    # Wait for TCP connection (glasses connects back to our server)
    ev = events.wait_for(type="STATE", timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=1)
        if ev and ev.get("tcp_connected"):
            break
    assert ev is not None and ev.get("tcp_connected"), (
        "TCP connection not established. Check firewall: "
        "sudo /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate"
    )

    cmd(proc, "wifi switch")
    ev = events.wait_for(type="WIFI", event="SWITCHED", timeout=8)
    assert ev is not None, "WiFi path switch (SWITCHED) not confirmed"

    state = events.wait_for(type="STATE", timeout=3)
    assert state is not None and state.get("wifi_active"), "wifi_active not True after switch"


def test_wifi_bt_fallback(proc, events: EventStream):
    """wifi bt → WIFI DROPPED / wifi_active=False."""
    assert _wait_phase5(events), "Did not reach phase5"
    cmd(proc, "wifi bt")
    deadline = time.time() + 5
    found = False
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=1)
        if ev and not ev.get("wifi_active"):
            found = True
            break
    assert found, "wifi_active not False after 'wifi bt'"
