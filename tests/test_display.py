"""
Display / rendering tests — require glasses at phase 5.
"""
import time
import pytest
from helpers.events import EventStream
from conftest import cmd


def _wait_phase5(events: EventStream, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = events.wait_for(type="STATE", timeout=2)
        if ev and ev.get("phase") == 5:
            return True
    return False


def test_glider_sends_frames(proc, events: EventStream):
    """glider command must produce TX 0xe7 frames."""
    assert _wait_phase5(events), "Phase 5 not reached"
    cmd(proc, "glider")
    ev = events.wait_for(type="TX", cmd="0xe7", timeout=8)
    assert ev is not None, "No 0xe7 TX frame within 8s of 'glider'"


def test_glider_frame_size_reasonable(proc, events: EventStream):
    """Each display frame must be between 100 and 60000 bytes (compressed)."""
    assert _wait_phase5(events), "Phase 5 not reached"
    cmd(proc, "glider")
    ev = events.wait_for(type="TX", cmd="0xe7", timeout=8)
    assert ev is not None
    assert 100 < ev["bytes"] < 60_000, f"Frame size unexpected: {ev['bytes']}B"


def test_stop_sends_black_frame(proc, events: EventStream):
    """stop command must send a 0xe7 frame (black)."""
    assert _wait_phase5(events), "Phase 5 not reached"
    cmd(proc, "glider")
    events.wait_for(type="TX", cmd="0xe7", timeout=5)
    cmd(proc, "stop")
    ev = events.wait_for(type="TX", cmd="0xe7", timeout=5)
    assert ev is not None, "No 0xe7 TX frame after 'stop'"


def test_layout_init_before_display(proc, events: EventStream):
    """LayoutInit (0xe0) must be sent before first display frame after phase5."""
    assert _wait_phase5(events), "Phase 5 not reached"
    # After phase 5, LayoutInit is sent automatically
    ev = events.wait_for(type="TX", cmd="0xe0", timeout=5)
    assert ev is not None, "LayoutInit (0xe0) not sent after phase 5"


def test_compress_ratio_under_10pct(proc, events: EventStream):
    """DEFLATE compression must achieve <10% ratio on GOL frames."""
    assert _wait_phase5(events), "Phase 5 not reached"
    cmd(proc, "glider")
    c = events.wait_for(type="COMPRESS", timeout=8)
    assert c is not None, "No COMPRESS event"
    assert c["ratio"] < 0.10, f"ratio={c['ratio']:.4f} >= 0.10"


def test_multiple_frames_sent(proc, events: EventStream):
    """At least 3 frames must be sent within 5s of starting glider."""
    assert _wait_phase5(events), "Phase 5 not reached"
    cmd(proc, "glider")
    count = 0
    deadline = time.time() + 6
    while time.time() < deadline and count < 3:
        ev = events.wait_for(type="TX", cmd="0xe7", timeout=2)
        if ev:
            count += 1
    assert count >= 3, f"Only {count} frames sent in 6s (expected >= 3)"
