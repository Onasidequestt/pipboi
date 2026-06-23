import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from bot_config import DATA_DIR
DB_PATH = DATA_DIR / "trades.db"
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    TEXT NOT NULL,
                event TEXT NOT NULL,
                data  TEXT NOT NULL
            )
        """)
        _conn.commit()
    return _conn


def _write(record: dict) -> None:
    record["ts"] = datetime.utcnow().isoformat()
    event = record.get("event", "unknown")
    db = _db()
    db.execute(
        "INSERT INTO trades (ts, event, data) VALUES (?, ?, ?)",
        (record["ts"], event, json.dumps(record)),
    )
    db.commit()
    print(f"[Audit] {record}", file=sys.stderr)


def log_signal(mint: str, momentum: float, price: float, rationale: str, vol_5m: Optional[float] = None) -> None:
    record: dict = {"event": "signal", "mint": mint, "momentum": momentum, "price": price, "rationale": rationale}
    if vol_5m is not None:
        record["vol_5m"] = round(vol_5m, 2)
    _write(record)


def log_risk_reject(mint: str, reason: str) -> None:
    _write({"event": "risk_reject", "mint": mint, "reason": reason})


def log_trade_open(mint: str, size_usd: float, price: float, signature: str, size_sol: float = 0.0, tier: Optional[str] = None, quote_out: int = 0, quote_in: int = 0, **extra) -> None:
    # S89: accept **extra (mode/play/regime/…) and persist it, mirroring log_trade_close. The
    # caller dispatches the full record via log_trade_open(**record); without **extra it raised
    # TypeError ("unexpected keyword argument 'mode'") on EVERY open → the `open` audit row was
    # dropped (why open events stopped recording). Non-fatal (background auditor) but lossy.
    record: dict = {"event": "open", "mint": mint, "size_sol": round(size_sol, 6), "size_usd": size_usd, "price": price, "sig": signature}
    if tier is not None:
        record["tier"] = tier
    if quote_out and quote_in:
        record["quote_out"] = quote_out
        record["quote_in"]  = quote_in
    record.update(extra)
    _write(record)


def log_trade_close(
    mint: str,
    pnl: float,
    signature: str,
    pnl_sol: float = 0.0,
    tier: Optional[str] = None,
    **extra,
) -> None:
    record: dict = {"event": "close", "mint": mint, "pnl": pnl, "pnl_sol": round(pnl_sol, 6), "sig": signature}
    if tier is not None:
        record["tier"] = tier
    # Persist any extra fields (cost_to_prestige, exec_penalty, etc.) into the DB row.
    record.update(extra)
    _write(record)


def log_error(context: str, error: str, **extra) -> None:
    # S89: absorb extra kwargs (e.g. detail=) so log_error(**record) can't raise a TypeError
    # and drop the error row.
    record: dict = {"event": "error", "context": context, "error": error}
    record.update(extra)
    _write(record)


def log_halt(reason: str) -> None:
    _write({"event": "halt", "reason": reason})


def log_tx_result(mint: str, sig: str, confirmed: bool, latency_ms: int) -> None:
    _write({"event": "tx_result", "mint": mint, "sig": sig[:20], "confirmed": confirmed, "latency_ms": latency_ms})


def get_revert_rate(lookback_minutes: int = 10, min_samples: int = 3,
                    session_start: str = "") -> float:
    """
    Fraction of failed confirmations in the last `lookback_minutes` minutes,
    but never before `session_start` (so a restart clears all prior failures).
    Returns 0.0 if fewer than min_samples exist in the window.
    """
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(minutes=lookback_minutes)).isoformat()
        # Use whichever is more recent: the time window or the session start
        if session_start and session_start > cutoff:
            cutoff = session_start
        rows = _db().execute(
            "SELECT data FROM trades WHERE event = 'tx_result' AND ts >= ? ORDER BY id DESC",
            (cutoff,),
        ).fetchall()
        if len(rows) < min_samples:
            return 0.0
        results = [json.loads(r[0]).get("confirmed", True) for r in rows]
        return sum(1 for r in results if not r) / len(results)
    except Exception:
        return 0.0
