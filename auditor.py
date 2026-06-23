"""
Tier 3: The Auditor — asynchronous trade-event processor.

The main trading loop calls push() after every trade event (open, close, signal, etc.).
push() is non-blocking — it enqueues and returns immediately.
consume_loop() runs as a background asyncio task and drains the queue.

The trading loop never waits for DB writes or goldilocks. Latency is eliminated.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import deque
from typing import Optional

try:
    from bot_config import BOT_ID as _BOT_ID
except Exception:
    _BOT_ID = 0

import audit as _audit
import memory


class Auditor:
    """
    Tier 3: processes trade events in background so main loop never blocks on I/O.

    Usage:
        _auditor = Auditor()
        asyncio.create_task(_auditor.consume_loop())   # start at bot startup
        _auditor.push("trade_open",  {...})             # non-blocking, from main loop
        _auditor.push("trade_close", {...})
    """

    _MAX_QUEUE = 2000   # safety cap — prevent unbounded growth

    def __init__(self) -> None:
        # Queue created lazily in consume_loop() — Python 3.9 binds Queue to the
        # event loop at __init__ time, but asyncio.run() creates a different loop.
        # Creating it here would give "Future attached to a different loop" on every get().
        self._queue:               Optional[asyncio.Queue] = None
        self._processed_session:   int                   = 0
        self._db_writes_session:   int                   = 0
        self._drops_session:       int                   = 0
        self._last_write_ts:       Optional[float]       = None
        self._last_goldilocks_ts:  Optional[float]       = None
        self._goldilocks_count:    int                   = 0   # closes since last goldilocks trigger
        self._ev_halt_streak:      int                   = 0
        self._last_event_type:     str                   = "—"
        # Command Ticker — last 20 human-readable events for dashboard display
        self._recent_events:       deque                 = deque(maxlen=20)

    # ── Non-blocking push (called from main loop) ──────────────────────────────

    def push(self, event_type: str, data: dict) -> None:
        """
        Enqueue a trade event for background processing.
        Drops silently if queue is full or not yet initialised.
        """
        if self._queue is None:
            self._drops_session += 1
            return
        record = {"_type": event_type, "_ts": time.time(), **data}
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._drops_session += 1

    # ── Background consumer ───────────────────────────────────────────────────

    async def consume_loop(self) -> None:
        """
        Background asyncio task. Start with asyncio.create_task().
        Drains the queue and writes to SQLite via audit.py.
        """
        # Create the queue here, inside the running event loop (Python 3.9 compat).
        self._queue = asyncio.Queue(maxsize=self._MAX_QUEUE)
        while True:
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=2.0)
                await self._process(record)
                self._queue.task_done()
            except asyncio.TimeoutError:
                pass   # idle — loop again
            except Exception as exc:
                import sys
                print(f"[Auditor] Error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(1)  # prevent spin-loop if something keeps raising

    async def _process(self, record: dict) -> None:
        etype = record.pop("_type", "write")
        record.pop("_ts", None)

        self._processed_session += 1
        self._last_event_type   = etype

        for _attempt in range(5):
            try:
                self._dispatch(etype, record)
                te = self._make_ticker_event(etype, record)
                if te:
                    self._recent_events.append(te)
                return
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and _attempt < 4:
                    await asyncio.sleep(0.05 * (_attempt + 1))   # 50ms … 200ms back-off
                else:
                    import sys
                    print(f"[Auditor] DB locked — dropping {etype}: {exc}", file=sys.stderr, flush=True)
                    return

    def _dispatch(self, etype: str, record: dict) -> None:
        """Synchronous dispatch — raises sqlite3.OperationalError on DB lock."""
        if etype == "trade_open":
            _audit.log_trade_open(**record)
            self._db_writes_session += 1
            self._last_write_ts = time.time()
        elif etype == "trade_close":
            # Extract exec_penalty before DB write so it doesn't cause a
            # kwarg collision — log_trade_close now accepts **extra so it
            # will be persisted, but we handle the side-effect here.
            _exec_pen = record.get("exec_penalty", 0.0)
            _audit.log_trade_close(**record)
            self._db_writes_session += 1
            self._last_write_ts = time.time()
            self._goldilocks_count += 1
            # Slippage-integrity feedback: accumulate penalty for tokens with
            # consistently bad fills.  Decay every 20 closes (~20-60 min).
            if _exec_pen > 0.0 and record.get("mint"):
                memory.update_execution_penalty(record["mint"], _exec_pen)
            if self._goldilocks_count % 20 == 0:
                memory.decay_execution_penalties()
        elif etype == "signal":
            _audit.log_signal(**record)
            self._db_writes_session += 1
            self._last_write_ts = time.time()
        elif etype == "risk_reject":
            _audit.log_risk_reject(**record)
        elif etype == "tx_result":
            _audit.log_tx_result(**record)
            self._db_writes_session += 1
            self._last_write_ts = time.time()
        elif etype == "halt":
            _audit.log_halt(**record)
        elif etype == "error":
            _audit.log_error(**record)
        elif etype == "ev_halt_streak":
            self._ev_halt_streak = record.get("streak", 0)
        elif etype == "goldilocks_ran":
            self._last_goldilocks_ts = record.get("ts", time.time())
            self._goldilocks_count   = 0
        else:
            # Generic write — caller passes full record dict with "event" key
            _audit._write(record)
            self._db_writes_session += 1
            self._last_write_ts = time.time()

    # ── Command Ticker event builder ───────────────────────────────────────────

    def _make_ticker_event(self, etype: str, record: dict) -> Optional[dict]:
        """Build a single human-readable event dict for the dashboard Command Ticker.
        Returns None to suppress an event type from the ticker."""
        tag = f"#{_BOT_ID}"

        if etype == "trade_open":
            mint  = (record.get("mint") or "?")[:8]
            size  = record.get("size_sol") or 0.0
            tier  = (record.get("tier") or "").upper()
            return {"ts": time.time(), "priority": "teal",
                    "label": "[BUY]",
                    "msg": f"BOT{tag} {mint}.. ◎{size:.3f}{(' '+tier) if tier else ''}"}

        if etype == "trade_close":
            mint = (record.get("mint") or "?")[:8]
            pnl  = record.get("pnl_sol") or 0.0
            sign = "+" if pnl >= 0 else ""
            return {"ts": time.time(), "priority": "teal",
                    "label": "[SELL]",
                    "msg": f"BOT{tag} {mint}.. {sign}{pnl:.4f}◎"}

        if etype == "halt":
            reason = (record.get("reason") or "")[:38]
            return {"ts": time.time(), "priority": "critical",
                    "label": "[HALT]",
                    "msg": f"BOT{tag} {reason}"}

        if etype == "goldilocks_ran":
            return {"ts": time.time(), "priority": "info",
                    "label": "[GOLDILOCKS]",
                    "msg": f"BOT{tag} optimizer ran"}

        if etype == "error":
            ctx = (record.get("context") or "?")[:6]
            err = (record.get("error") or "")[:28]
            return {"ts": time.time(), "priority": "info",
                    "label": "[ERR]",
                    "msg": f"BOT{tag} {ctx}.. {err}"}

        return None

    # ── Dashboard snapshot ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        last_write_ago: Optional[int] = None
        if self._last_write_ts:
            last_write_ago = int(time.time() - self._last_write_ts)

        last_gl_ago: Optional[int] = None
        if self._last_goldilocks_ts:
            last_gl_ago = int(time.time() - self._last_goldilocks_ts)

        return {
            "queue_depth":           self._queue.qsize() if self._queue else 0,
            "processed_session":     self._processed_session,
            "db_writes_session":     self._db_writes_session,
            "drops_session":         self._drops_session,
            "last_write_ago_s":      last_write_ago,
            "last_goldilocks_ago_s": last_gl_ago,
            "goldilocks_count":      self._goldilocks_count,
            "ev_halt_streak":        self._ev_halt_streak,
            "last_event_type":       self._last_event_type,
            # Command Ticker — last 20 events for dashboard display
            "recent_events":         list(self._recent_events),
        }
