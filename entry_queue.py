"""
EntryQueue — decouples signal discovery from trade execution.

Discovery sizes signals and pushes them here.
Execution pops one at a time, with a mandatory 5-second cooldown between
consecutive buys to prevent rate-limiting on Jupiter quote and Jito bundle endpoints.

Guarantees:
  - At most one execute_buy() in-flight at a time (_busy flag).
  - Minimum COOLDOWN_SECS gap between the END of one trade and the START of the next.
  - Signals older than MAX_SIGNAL_AGE are silently discarded on get() — prices
    will have moved past the entry thesis by then.
  - Queue is bounded (maxsize); oldest entry is evicted if full, so a starvation
    period never causes unbounded accumulation.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

COOLDOWN_SECS  = 5.0    # seconds to wait after each trade before the next
MAX_SIGNAL_AGE = 60.0   # discard signals not consumed within this window
_MAX_SIZE      = 50     # hard cap — prevents runaway accumulation


class EntryQueue:
    def __init__(
        self,
        cooldown_secs: float = COOLDOWN_SECS,
        max_signal_age: float = MAX_SIGNAL_AGE,
        maxsize: int = _MAX_SIZE,
    ) -> None:
        # (enqueued_at: float, signal: dict)
        self._q: deque = deque()
        self._maxsize      = maxsize
        self._cooldown     = cooldown_secs
        self._max_age      = max_signal_age
        self._busy         = False
        self._next_allowed = 0.0    # monotonic time after which next trade may start

    # ── Producer (Discovery / sizing loop) ───────────────────────────────────

    def put(self, signal: dict) -> None:
        """Enqueue a sized signal. Evicts the oldest entry if at capacity."""
        if len(self._q) >= self._maxsize:
            self._q.popleft()
        self._q.append((time.monotonic(), signal))

    def put_front(self, signal: dict) -> None:
        """Insert a signal at the FRONT so it executes next.
        Used for Force-FIRE signals that must execute before queued normal signals.
        Evicts the oldest entry (from the back) if at capacity so high-priority
        signals always displace stale ones, not each other.
        """
        if len(self._q) >= self._maxsize:
            self._q.pop()   # evict oldest from back, not from front
        self._q.appendleft((time.monotonic(), signal))

    # ── Consumer (Execution loop) ─────────────────────────────────────────────

    def ready(self) -> bool:
        """True when it is safe to start the next trade."""
        return not self._busy and time.monotonic() >= self._next_allowed

    def cooldown_remaining(self) -> float:
        """Seconds until cooldown expires. 0.0 if already past it."""
        return max(0.0, self._next_allowed - time.monotonic())

    def get(self) -> Optional[dict]:
        """Pop the next fresh signal, or None if empty / not ready / all stale."""
        if not self.ready():
            return None
        now = time.monotonic()
        while self._q:
            enqueued_at, sig = self._q.popleft()
            if now - enqueued_at <= self._max_age:
                return sig
            # stale — price thesis is void, drop silently
        return None

    def mark_busy(self) -> None:
        """Call immediately before execute_buy() begins."""
        self._busy = True

    def mark_done(self) -> None:
        """Call when execute_buy() finishes (success or failure).
        Starts the cooldown window.
        """
        self._busy = False
        self._next_allowed = time.monotonic() + self._cooldown

    # ── Introspection ─────────────────────────────────────────────────────────

    def depth(self) -> int:
        return len(self._q)

    def snapshot(self) -> dict:
        """Serialisable state for status.json / Think Log."""
        now = time.monotonic()
        return {
            "depth":              self.depth(),
            "busy":               self._busy,
            "cooldown_remaining": round(self.cooldown_remaining(), 1),
            "pending":            [
                sig.get("mint", "?")[:8]
                for ts, sig in list(self._q)
                if now - ts <= self._max_age
            ][:8],
        }
