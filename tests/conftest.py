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


@pytest.fixture
def proc(events: EventStream):
    """Spawn glasses-tool connect. Requires powered-on glasses."""
    events.clear()
    p = subprocess.Popen(
        [GLASSES_TOOL, "connect"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=REPO,
    )
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
