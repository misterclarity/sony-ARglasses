import json
import time
from pathlib import Path


class EventStream:
    """Tail /tmp/glasses-events.jsonl and match events."""

    def __init__(self, path: str = "/tmp/glasses-events.jsonl"):
        self.path = path

    def wait_for(
        self,
        type: str | None = None,
        cmd: str | None = None,
        event: str | None = None,
        timeout: float = 10.0,
    ) -> dict | None:
        """Block until a matching event arrives or timeout expires.
        Returns the event dict or None on timeout.
        """
        deadline = time.time() + timeout
        p = Path(self.path)
        seen_size = p.stat().st_size if p.exists() else 0

        while time.time() < deadline:
            if p.exists():
                with open(self.path) as f:
                    f.seek(seen_size)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if type is not None and e.get("type") != type:
                            continue
                        if cmd is not None and e.get("cmd") != cmd:
                            continue
                        if event is not None and e.get("event") != event:
                            continue
                        return e
                    seen_size = f.tell()
            time.sleep(0.05)
        return None

    def all_since(self, since_ts: float = 0.0) -> list[dict]:
        """Return all events with ts >= since_ts."""
        p = Path(self.path)
        if not p.exists():
            return []
        events = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("ts", 0) >= since_ts:
                        events.append(e)
                except json.JSONDecodeError:
                    pass
        return events

    def clear(self):
        Path(self.path).write_text("")
