"""
pytest fixtures for SED-E1 hardware tests.

Requires physical glasses powered on and paired with this Mac.

Setup:
    uv sync                                  # install deps

Run:
    uv run pytest tests/ -v
    uv run pytest tests/test_protocol.py -v  # BT only, no WiFi needed
    uv run pytest tests/test_wifi.py -v      # WiFi: needs same-network setup
      → put SSID/PSWD in macos-middleware/.env and run ./glasses-wifi-setup.sh
"""
import subprocess
import time
import pytest
from helpers.events import EventStream

REPO = "/Users/gerhardgustav/Desktop/hobby-dev/sony-sed-e1"
GLASSES_TOOL = f"{REPO}/macos-middleware/glasses-tool"


@pytest.fixture(scope="session")
def events() -> EventStream:
    es = EventStream()
    es.clear()
    return es


@pytest.fixture(scope="module")
def proc(events: EventStream):
    """Spawn glasses-tool connect (one connection per test module).

    Auto-selects the first paired SmartEyeglass by writing '1\\n' to stdin
    immediately — this queues in the pipe buffer so readLine() picks it up
    the moment the device-selection prompt appears (after the 8s scan).

    Override with GLASSES_ADDR env var to skip the scan entirely:
        GLASSES_ADDR=ac:9b:0a:37:a6:c6 uv run pytest tests/ -v
    """
    import os
    events.clear()

    addr = os.environ.get("GLASSES_ADDR", "")
    cmd_args = [GLASSES_TOOL, "connect"] + ([addr] if addr else [])

    p = subprocess.Popen(
        cmd_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,  # stdout captured → pipe fills → deadlock; use JSON log instead
        stderr=subprocess.DEVNULL,
        cwd=REPO,
    )
    # Queue '1\n' so the multi-device selector auto-picks device #1
    # without blocking readLine() in glasses-tool.
    if not addr:
        try:
            p.stdin.write(b"1\n")  # type: ignore[union-attr]
            p.stdin.flush()
        except Exception:
            pass

    yield p

    try:
        if p.stdin:
            p.stdin.write(b"quit\n")
            p.stdin.flush()
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def cmd(proc: subprocess.Popen, s: str):
    """Send a REPL command to glasses-tool stdin."""
    assert proc.stdin is not None
    proc.stdin.write(f"{s}\n".encode())
    proc.stdin.flush()


@pytest.fixture(autouse=True)
def clear_events_before_test(events: EventStream):
    """Clear the event log before each test so wait_for() sees fresh events."""
    events.clear()
