"""Background loops that are restarted if they crash or hang is detected by health()."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Callable

from .db import DB

log = logging.getLogger("mytv")


class Supervisor:
    def __init__(self, db: DB):
        self.db = db
        self.stop_event = threading.Event()
        self.loops: dict[str, dict] = {}

    def add(self, name: str, fn: Callable[[], float | None], every: float, stale_after: float) -> None:
        self.loops[name] = {"fn": fn, "every": every, "stale": stale_after, "beat": time.time(), "restarts": 0, "error": None}
        self._start(name)

    def _start(self, name: str) -> None:
        t = threading.Thread(target=self._loop, args=(name,), daemon=True, name=name)
        self.loops[name]["thread"] = t
        t.start()

    def _loop(self, name: str) -> None:
        st = self.loops[name]
        while not self.stop_event.is_set():
            st["beat"] = time.time()
            wait = st["every"]
            try:
                r = st["fn"]()
                wait = st["every"] if r is None else r
                st["error"] = None
            except Exception as e:
                st["restarts"] += 1
                st["error"] = f"{type(e).__name__}: {e}"
                log.error("%s crashed:\n%s", name, traceback.format_exc())
                try:
                    self.db.event("error", name, f"Background task error, restarting: {st['error']}"[:300])
                except Exception:
                    pass
                wait = min(60, 2 * st["restarts"])
            st["beat"] = time.time()
            self.stop_event.wait(wait)

    def watch(self) -> None:
        """Called from a request or timer: revive any loop whose thread died."""
        for name, st in self.loops.items():
            if not st["thread"].is_alive() and not self.stop_event.is_set():
                st["restarts"] += 1
                self._start(name)

    def status(self) -> dict:
        now = time.time()
        return {n: {"ok": s["thread"].is_alive() and now - s["beat"] < s["stale"] and not s["error"],
                    "last_beat": s["beat"], "restarts": s["restarts"], "error": s["error"]} for n, s in self.loops.items()}
