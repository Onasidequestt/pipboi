"""
VAULT FLEET — Dashboard & Process Manager
One dashboard runs all bots. Bots are headless processes managed here.
"""
import asyncio
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from solders.keypair import Keypair

from config import HELIUS_RPC_URL

# ── Constants ─────────────────────────────────────────────────────────────────
BOTS_DIR           = Path("bots")
FLEET_PATH         = Path("fleet.json")
BOT_IDS            = [1, 2, 3, 4, 5, 6]
ACTIVATION_MIN_SOL = 0.1

# ── Auth ──────────────────────────────────────────────────────────────────────
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip()
DASHBOARD_PIN    = os.getenv("DASHBOARD_PIN",    "0098").strip()
_auth_failures: dict[str, list] = {}   # ip → [timestamp, ...]
# 5 failures per 60-second window. At 5/min a 4-digit PIN takes ~33 hours minimum,
# and the progressive lockout below stretches that to days for persistent attackers.
_AUTH_MAX_FAILURES = 5
_AUTH_WINDOW_SEC   = 60

def _require_auth(request: Request) -> None:
    """Raise 401/429 if the request lacks a valid credential.
    Accepts either DASHBOARD_SECRET (strong token) or DASHBOARD_PIN (short PIN).
    Progressive lockout: each batch of 5 failures doubles the cooldown window.
    """
    if not DASHBOARD_SECRET:
        return  # no secret configured — open dev mode
    ip = (request.client.host if request.client else "unknown")
    now = time.time()
    all_failures = _auth_failures.get(ip, [])
    # Progressive window: doubles every 5 failures (5→120s, 10→240s, 15→480s …)
    batch = len(all_failures) // _AUTH_MAX_FAILURES
    window = _AUTH_WINDOW_SEC * (2 ** batch)
    recent = [t for t in all_failures if now - t < window]
    if len(recent) >= _AUTH_MAX_FAILURES:
        wait = int(window - (now - min(recent)))
        raise HTTPException(status_code=429, detail=f"Too many failed attempts — wait {wait}s")
    token = request.headers.get("X-Dashboard-Secret", "").strip()
    # Timing-safe comparison — prevents timing-oracle brute-force against the secret.
    _tok_b   = token.encode()
    secret_ok = bool(DASHBOARD_SECRET) and hmac.compare_digest(_tok_b, DASHBOARD_SECRET.encode())
    pin_ok    = bool(DASHBOARD_PIN)    and hmac.compare_digest(_tok_b, DASHBOARD_PIN.encode())
    if secret_ok or pin_ok:
        _auth_failures[ip] = []   # reset on success
        # Prune stale IPs periodically to prevent unbounded dict growth
        if len(_auth_failures) > 500:
            cutoff = now - _AUTH_WINDOW_SEC * 64  # keep up to 6 doublings worth
            stale = [k for k, v in _auth_failures.items() if not v or max(v) < cutoff]
            for k in stale:
                del _auth_failures[k]
        return
    # Cap per-IP list to prevent unbounded memory growth under flood attacks
    _auth_failures[ip] = (all_failures + [now])[-100:]
    raise HTTPException(status_code=401, detail="Unauthorized")


def _validate_bot(bot_id: int) -> None:
    """Raise 400 if bot_id is not in the valid set."""
    if bot_id not in BOT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid bot_id: {bot_id}")


def _is_authed(request: Request) -> bool:
    """Non-raising auth check. Returns True if the request carries a valid credential."""
    if not DASHBOARD_SECRET:
        return True  # open dev mode — no secret configured
    token = request.headers.get("X-Dashboard-Secret", "").strip()
    if not token:
        return False
    b = token.encode()
    return (hmac.compare_digest(b, DASHBOARD_SECRET.encode()) or
            (bool(DASHBOARD_PIN) and hmac.compare_digest(b, DASHBOARD_PIN.encode())))


# ── GET rate limiter ──────────────────────────────────────────────────────────
# Unauthenticated viewers (including thousands of spectators) must not be able to
# hammer Helius-backed endpoints or exhaust server resources. Authenticated admin
# requests are never rate-limited here.
_get_rate: dict[str, list] = {}
# S84: raised 30→120. The 30/min cap was too tight for the dashboard's OWN spectator
# polling: _autoPoll fires ~8 GETs/33s (fleet+status+history+trades × bots) ≈ 14/min,
# +/api/race 4/min when a GENE tab is open, +on-open interaction bursts. A spectator
# clicking through tabs blew past 30/min → /api/race 429'd → the GENE tab's _raceCache
# never populated → it stranded on its skeleton on all bots. 120/min still bounds abuse
# (admins are exempt above) while giving the dashboard's own cadence 3-4× headroom.
_GET_RATE_MAX    = 120   # requests per window per IP (spectator only; admins uncapped)
_GET_RATE_WINDOW = 60.0  # sliding window in seconds

def _check_get_rate(request: Request) -> None:
    if _is_authed(request):
        return  # admin — no cap
    ip  = request.client.host if request.client else "unknown"
    now = time.time()
    ts  = [t for t in _get_rate.get(ip, []) if now - t < _GET_RATE_WINDOW]
    if len(ts) >= _GET_RATE_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down polling")
    ts.append(now)
    _get_rate[ip] = ts[-_GET_RATE_MAX:]
    # Periodic pruning — prevent unbounded dict growth under sustained traffic
    if len(_get_rate) > 5000:
        cutoff = now - _GET_RATE_WINDOW
        for k in [k for k, v in _get_rate.items() if not v or max(v) < cutoff]:
            del _get_rate[k]


def _sanitize_live(live: Optional[dict], authed: bool) -> Optional[dict]:
    """Strip fields that could enable front-running or strategy inference for public viewers.

    hot_list: tokens the bot has scored highly and may buy next cycle — actionable for front-runners.
    token_memory: per-token win rates and confidence scores — reveals strategy intelligence.
    """
    if live is None or authed:
        return live
    safe = {k: v for k, v in live.items() if k != "token_memory"}
    if "thinking" in safe:
        t = {**safe["thinking"]}
        if "tier1_observer" in t:
            obs = {**t["tier1_observer"]}
            obs["hot_list"] = []  # front-running prevention — which tokens passed scoring
            t["tier1_observer"] = obs
        safe["thinking"] = t
    return safe


# Live bot processes — bot_id → Popen
_procs: dict[int, subprocess.Popen] = {}

# Persisted PIDs — written on spawn, read on next startup to kill orphans
_PIDS_PATH = Path("bot_pids.json")


def _save_pids() -> None:
    _tmp = _PIDS_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps({str(k): v.pid for k, v in _procs.items()}))
    os.replace(_tmp, _PIDS_PATH)


def _kill_orphans() -> None:
    """Kill any bot processes left over from a previous dashboard session."""
    if not _PIDS_PATH.exists():
        return
    try:
        old = json.loads(_PIDS_PATH.read_text())
    except Exception:
        return
    for bot_id, pid in old.items():
        try:
            os.kill(pid, 0)          # check if process is alive (raises if not)
            os.kill(pid, 15)         # SIGTERM
            print(f"[Fleet] Killed orphaned Bot #{bot_id} (PID {pid})", flush=True)
        except ProcessLookupError:
            pass                     # already dead, nothing to do
        except Exception as e:
            print(f"[Fleet] Could not kill PID {pid}: {e}", flush=True)
    _PIDS_PATH.unlink(missing_ok=True)


# ── Fleet state ───────────────────────────────────────────────────────────────

def _default_fleet() -> dict:
    bots = []
    for i in BOT_IDS:
        if i <= 3:
            # Bots 1-3 start undeployed. Deploy a bot from the dashboard (generates a
            # keypair) or point KEYPAIR_PATH in your .env at an existing wallet.
            b = {"id": i, "status": "undeployed", "pubkey": None,
                 "keypair_path": None, "data_dir": f"bots/bot{i}"}
        else:
            b = {"id": i, "status": "row2_locked", "pubkey": None,
                 "keypair_path": None, "data_dir": f"bots/bot{i}"}
        bots.append(b)
    return {"bots": bots, "row2_unlocked": False}


def _migrate_fleet(fleet: dict) -> dict:
    """Add bots 4-6 as row2_locked and row2_unlocked flag if missing from existing fleet.json."""
    existing_ids = {b["id"] for b in fleet["bots"]}
    changed = False
    for i in range(4, 7):
        if i not in existing_ids:
            fleet["bots"].append({"id": i, "status": "row2_locked",
                                  "pubkey": None, "keypair_path": None,
                                  "data_dir": f"bots/bot{i}"})
            changed = True
    if "row2_unlocked" not in fleet:
        fleet["row2_unlocked"] = False
        changed = True
    if changed:
        save_fleet(fleet)
    return fleet


def load_fleet() -> dict:
    if FLEET_PATH.exists():
        try:
            fleet = json.loads(FLEET_PATH.read_text())
            return _migrate_fleet(fleet)
        except Exception:
            pass
    return _default_fleet()


def save_fleet(fleet: dict) -> None:
    _tmp = FLEET_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps(fleet, indent=2))
    os.replace(_tmp, FLEET_PATH)


def bot_dir(bot_id: int) -> Path:
    return BOTS_DIR / f"bot{bot_id}"


def fleet_entry(bot_id: int) -> Optional[dict]:
    return next((b for b in load_fleet()["bots"] if b["id"] == bot_id), None)


# ── Bot 1 migration ───────────────────────────────────────────────────────────

def _migrate_bot1() -> None:
    """Move root-level state files into bots/bot1/ on first run after upgrade."""
    target = bot_dir(1)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("positions.json", "payout_wallet.json", "trading_mode.json",
                 "marketcap_mode.json", "trades.db", "balance_history.jsonl",
                 "trade_memory.json", "status.json"):
        src = Path(name)
        dst = target / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


# ── Keypair helpers ───────────────────────────────────────────────────────────

def _load_keypair(path: str) -> Keypair:
    expanded = os.path.expanduser(path)
    with open(expanded) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


# ── Audit write (direct sqlite, no module state) ──────────────────────────────

def _audit_write(bot_id: int, record: dict) -> None:
    record["ts"] = datetime.utcnow().isoformat()
    db_path = bot_dir(bot_id) / "trades.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS trades
            (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
             event TEXT NOT NULL, data TEXT NOT NULL)""")
        conn.execute("INSERT INTO trades (ts, event, data) VALUES (?,?,?)",
                     (record["ts"], record.get("event", "unknown"), json.dumps(record)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Fleet] Audit write error (bot {bot_id}): {e}")


# ── Process management ────────────────────────────────────────────────────────

def _spawn(bot_id: int) -> None:
    existing = _procs.get(bot_id)
    if existing and existing.poll() is None:
        return  # already running
    env = {**os.environ, "BOT_ID": str(bot_id)}
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        env=env,
        cwd=str(Path(__file__).parent),
    )
    _procs[bot_id] = proc
    _save_pids()
    print(f"[Fleet] Bot #{bot_id} started — PID {proc.pid}", flush=True)


async def _startup() -> None:
    _kill_orphans()
    _migrate_bot1()
    if not DASHBOARD_SECRET:
        print("", flush=True)
        print("┌─────────────────────────────────────────────────────────────┐", flush=True)
        print("│  ⚠⚠⚠  SECURITY WARNING — DASHBOARD IS UNPROTECTED  ⚠⚠⚠  │", flush=True)
        print("│  DASHBOARD_SECRET is not set in .env                        │", flush=True)
        print("│  ALL admin controls (pause, drain, mode changes) are        │", flush=True)
        print("│  accessible to ANYONE who can reach port 8080.              │", flush=True)
        print("│  Add DASHBOARD_SECRET=<random-token> to .env NOW.           │", flush=True)
        print("└─────────────────────────────────────────────────────────────┘", flush=True)
        print("", flush=True)
    elif DASHBOARD_PIN == "0098":
        # The default PIN is public knowledge. Refuse to start with it when a real
        # secret is configured — anyone who reads this source can try "0098" first.
        print("[Fleet] ✗ FATAL: DASHBOARD_PIN is still the default '0098'. Set a real PIN in .env.", flush=True)
        print("[Fleet]   Generate one: python3 -c \"import secrets; print(secrets.token_hex(6))\"", flush=True)
        import sys as _sys; _sys.exit(1)
    for bot in load_fleet()["bots"]:
        if bot["status"] == "active":
            _spawn(bot["id"])


# ── Funding poll loop ─────────────────────────────────────────────────────────

async def _funding_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(15)
            try:
                fleet = load_fleet()
                changed = False
                for bot in fleet["bots"]:
                    if bot["status"] != "pending_activation" or not bot.get("pubkey"):
                        continue
                    r = await client.post(HELIUS_RPC_URL, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance", "params": [bot["pubkey"]],
                    }, timeout=10)
                    sol = r.json().get("result", {}).get("value", 0) / 1e9
                    if sol >= ACTIVATION_MIN_SOL:
                        print(f"[Fleet] Bot #{bot['id']} funded ◎{sol:.4f} — activating", flush=True)
                        bot["status"] = "active"
                        changed = True
                        _spawn(bot["id"])
                if changed:
                    save_fleet(fleet)
            except Exception as e:
                print(f"[Fleet] Funding poll error: {e}", flush=True)


# ── Bot watchdog loop ─────────────────────────────────────────────────────────

async def _watchdog_loop() -> None:
    """Restart any active bot process that has crashed."""
    while True:
        await asyncio.sleep(30)
        try:
            for bot in load_fleet()["bots"]:
                if bot["status"] != "active":
                    continue
                bot_id = bot["id"]
                proc = _procs.get(bot_id)
                if proc is not None and proc.poll() is not None:
                    print(f"[Fleet] Bot #{bot_id} crashed (exit {proc.poll()}) — restarting", flush=True)
                    _spawn(bot_id)
        except Exception as e:
            print(f"[Fleet] Watchdog error: {e}", flush=True)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    task = asyncio.create_task(_funding_loop())
    watchdog = asyncio.create_task(_watchdog_loop())
    yield
    watchdog.cancel()
    task.cancel()
    # Shut down all bot processes — SIGTERM first, SIGKILL if they don't exit in 2s.
    # Positions are already persisted to positions.json so no state is lost.
    for proc in _procs.values():
        if proc.poll() is None:
            proc.terminate()
    for proc in _procs.values():
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()  # force-kill anything still alive
    _PIDS_PATH.unlink(missing_ok=True)  # clean up PID file on orderly exit


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]        = "no-referrer"
        # Live trading data must never be served stale from a cache. Immutable brand
        # assets keep the long-cache header the asset route already set.
        _p = request.url.path
        if not (_p.startswith("/assets/") or _p == "/favicon.ico"):
            response.headers["Cache-Control"] = "no-store"
        # CSP: inline scripts/styles required (large single-file dashboard).
        # connect-src 'self' blocks JS from sending data to attacker-controlled hosts
        # even if an XSS fires. frame-ancestors replaces X-Frame-Options for modern browsers.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
            "font-src fonts.gstatic.com; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

# docs_url=None — never expose the Swagger/ReDoc API explorer publicly.
# It maps every endpoint, parameter, and response schema to anyone who finds the URL.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(_SecurityHeaders)
# CORS: GET only from same origin. Cross-origin reads of bot data blocked by default.
# allow_origins=["*"] is intentional — ngrok URL changes each restart, so we can't
# pin a specific origin. All mutating endpoints require the X-Dashboard-Secret header
# which browsers won't attach cross-origin without a preflight (which we block via
# allow_methods=["GET"]). Sensitive GET data is already stripped for unauthenticated requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["X-Dashboard-Secret"],
    expose_headers=[],
)


# ── HTML ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    # First run / not configured yet → show the web setup page instead of an empty
    # dashboard, so a new user can enter their API keys right in the browser.
    if not _is_configured():
        return HTMLResponse(
            Path("templates/setup.html").read_text(),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    # No-cache on the dashboard HTML so UI edits to templates/index.html show on a NORMAL
    # refresh — no hard-refresh (Cmd+Shift+R) needed. The template is read per request anyway,
    # so the cost is nil; only the (tiny) HTML is uncached, static assets keep their long cache.
    return HTMLResponse(
        Path("templates/index.html").read_text(),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ── Brand assets ($Vault logo) ──────────────────────────────────────────────────
# Static, public, read-only. Allowlisted filenames only — no path traversal, no
# arbitrary file reads. Served same-origin so the CSP img-src 'self' permits them.
_ASSET_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
}
_ASSET_ALLOW = {
    "vault-logo.png", "vault-logo.svg",
    "vault-mark.png", "vault-mark.svg",
}


@app.get("/assets/{fname}")
async def asset(fname: str):
    if fname not in _ASSET_ALLOW:
        raise HTTPException(status_code=404, detail="not found")
    path = Path("assets") / fname
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = _ASSET_TYPES.get(path.suffix.lower(), "application/octet-stream")
    # Brand art is immutable; let browsers cache it (the no-store header is for live data).
    return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(Path("assets") / "vault-mark.png", media_type="image/png")


@app.post("/api/auth")
async def auth_check(request: Request):
    """Lightweight credential check — no side effects. Returns 200 if valid, 401/429 if not."""
    _require_auth(request)
    return JSONResponse({"ok": True})


# ── Web setup / onboarding ──────────────────────────────────────────────────
# Lets a new user configure their API keys from the browser (no terminal editing).
# Writes only to the local .env (gitignored). Whitelisted keys only.
_ENV_PATH = Path(".env")
_SETUP_FIELDS = {"HELIUS_API_KEY", "BITQUERY_API_KEY", "RPC_URL",
                 "KEYPAIR_PATH", "TOTAL_CAPITAL_USD"}


def _read_env_file() -> dict:
    cfg = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def _is_configured() -> bool:
    return bool(_read_env_file().get("HELIUS_API_KEY", "").strip())


def _write_env_updates(updates: dict) -> None:
    """Merge whitelisted updates into .env, preserving other keys. Atomic, 0600."""
    cfg = _read_env_file()
    cfg.update({k: v for k, v in updates.items() if k in _SETUP_FIELDS})
    order = ["HELIUS_API_KEY", "KEYPAIR_PATH", "TOTAL_CAPITAL_USD",
             "DASHBOARD_SECRET", "DASHBOARD_PIN", "RPC_URL", "BITQUERY_API_KEY"]
    lines = ["# Updated via the dashboard setup page — never commit this file."]
    written = set()
    for k in order:
        if cfg.get(k, "") != "":
            lines.append(f"{k}={cfg[k]}"); written.add(k)
    for k, v in cfg.items():
        if k not in written and v != "":
            lines.append(f"{k}={v}")
    tmp = _ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, _ENV_PATH)
    try:
        os.chmod(_ENV_PATH, 0o600)
    except OSError:
        pass


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return HTMLResponse(
        Path("templates/setup.html").read_text(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/setup/status")
async def setup_status(request: Request):
    _require_auth(request)
    cfg = _read_env_file()
    return JSONResponse({  # booleans + non-secret defaults only — never the key values
        "configured": bool(cfg.get("HELIUS_API_KEY", "").strip()),
        "helius":     bool(cfg.get("HELIUS_API_KEY", "").strip()),
        "bitquery":   bool(cfg.get("BITQUERY_API_KEY", "").strip()),
        "rpc":        bool(cfg.get("RPC_URL", "").strip()),
        "keypair_path": cfg.get("KEYPAIR_PATH", "~/.config/solana/vault-bot.json"),
        "capital":    cfg.get("TOTAL_CAPITAL_USD", "100.0"),
    })


@app.post("/api/setup")
async def setup_save(request: Request):
    _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
    updates = {}
    _map = {"helius": "HELIUS_API_KEY", "bitquery": "BITQUERY_API_KEY", "rpc": "RPC_URL",
            "keypair_path": "KEYPAIR_PATH", "capital": "TOTAL_CAPITAL_USD"}
    for src, dst in _map.items():
        if src in body and body[src] is not None:
            updates[dst] = str(body[src]).strip()
    # Helius is required (use the new value, else any existing one)
    helius = updates.get("HELIUS_API_KEY", _read_env_file().get("HELIUS_API_KEY", ""))
    if not helius.strip():
        return JSONResponse({"ok": False, "error": "A Helius API key is required."}, status_code=400)
    _write_env_updates(updates)
    return JSONResponse({"ok": True, "restart_required": True})


# ── Fleet endpoint ─────────────────────────────────────────────────────────────

@app.get("/api/fleet")
async def get_fleet(request: Request):
    _check_get_rate(request)
    authed = _is_authed(request)
    fleet = load_fleet()

    # Auto-unlock row 2 if any active bot has achieved first prestige (payout_milestones total > 0)
    if not fleet.get("row2_unlocked", False):
        for bot in fleet["bots"]:
            if bot["status"] == "active":
                sf = bot_dir(bot["id"]) / "status.json"
                if sf.exists():
                    try:
                        live = json.loads(sf.read_text())
                        total_payouts = sum(
                            m.get("payout_count", 0)
                            for m in live.get("payout_milestones", [])
                        )
                        if total_payouts > 0:
                            fleet["row2_unlocked"] = True
                            for b in fleet["bots"]:
                                if b.get("status") == "row2_locked":
                                    b["status"] = "undeployed"
                            # S80: GEN-2 SEEDING — copy the winner's PROVEN SIZE gene to the
                            # losers + bots 4-6 so the new fleet launches on the winning gene,
                            # not the default. Fires exactly once (guarded by row2_unlocked) and
                            # only on a genuine prestige (total_payouts>0) → honours the operator
                            # rule structurally. Exception-wrapped so it can never break /api/fleet.
                            try:
                                import gene_propagation as _gp
                                _wg, _rows = _gp.plan(bot["id"])
                                _n = _gp.propagate(bot["id"], _wg, _rows, "auto-unlock (first prestige)")
                                print(f"[Fleet] 🧬 Gene propagation — Bot #{bot['id']}'s gene → {_n} bot(s)", flush=True)
                            except Exception as _e:
                                print(f"[Fleet] gene propagation skipped: {_e}", flush=True)
                            save_fleet(fleet)
                            print(f"[Fleet] ⚡ Row 2 UNLOCKED — Bot #{bot['id']} achieved first prestige!", flush=True)
                            break
                    except Exception:
                        pass

    row2_unlocked = fleet.get("row2_unlocked", False)
    _STRIP = {"keypair_path", "data_dir"}   # never expose filesystem paths publicly
    result = []
    for bot in fleet["bots"]:
        entry = {k: v for k, v in bot.items() if k not in _STRIP}
        entry["row2_unlocked"] = row2_unlocked
        if bot["status"] == "active":
            sf = bot_dir(bot["id"]) / "status.json"
            raw_live = json.loads(sf.read_text()) if sf.exists() else None
            entry["live"] = _sanitize_live(raw_live, authed)
        else:
            entry["live"] = None
        result.append(entry)
    return JSONResponse(result)


# ── PRESTIGE RACE (gen-1 gene pool) — trajectory + genes + deploy gate ──────────
RACE_GOAL       = 2.0
RACE_BOTS       = (1, 2, 3)
RACE_MIN_CLOSES = 15
RACE_GHOST_MAX  = 0.10
RACE_DP_PLAYS   = {"deep_pool", "brain_rule"}
RACE_COLORS     = {1: "#00ff41", 2: "#ffcc00", 3: "#00ccff"}

# ── S97: top-of-dashboard LIVE SIGNAL feed (admin-only, read-only morale/visibility) ──
# Surfaces the highest-conviction "Jotchua-shape" candidate from the live discovery feed,
# scored by virality_probe.score (the S90 LEAD-edge lens). HARD-CAPPED to keep the dashboard
# "monitoring only, no noise": at most _SIGNAL_DAILY_CAP distinct signals promoted per UTC day,
# each must clear _SIGNAL_RUNNER_MIN (= the probe's "🔥 LIVE RUNNER" verdict). Dedup state lives
# in shared_memory/signal_feed.json (touches NOTHING the fleet owns — no bots/genes/trades.db).
_SIGNAL_RUNNER_MIN = 68.0          # virality score for the LIVE-RUNNER verdict
_SIGNAL_DAILY_CAP  = 3             # max distinct signals surfaced per day (1–3/day, capped)
_SIGNAL_STATE      = Path("shared_memory/signal_feed.json")


def _race_iso_ms(t):
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000.0
    except Exception:
        return None


def _race_gene(b):
    """Gene descriptor for bot b's SIZE gene. No ev_sizing.json → control.
    Returns (label, short, armed, scale, gen, parent): `scale` is the size
    multiplier (1.0 full / <1 moderate / None control), `gen` the generation
    (1 = origin gen-1 pool, ≥2 = propagated from a prestige winner), `parent`
    the bot this gene was propagated from (None for origin/control)."""
    try:
        f = json.loads((bot_dir(b) / "ev_sizing.json").read_text())
        if f.get("enabled"):
            gen    = int(f.get("gen", 1) or 1)
            parent = f.get("propagated_from")
            sc     = f.get("scale")
            if sc is not None and float(sc) < 1.0:
                return f"moderate EV-size ×{sc}", "moderate", True, float(sc), gen, parent
            return "full EV-size (aggressive)", "full", True, 1.0, gen, parent
    except Exception:
        pass
    return "C² flat (control)", "control", False, None, 1, None


def _race_total(b):
    try:
        s = json.loads((bot_dir(b) / "status.json").read_text())
        return float(s.get("sol_balance", 0) or 0) + float(s.get("sol_in_trades", 0) or 0)
    except Exception:
        return 0.0


def _race_series(b, since_ts, max_pts=90):
    pts = []
    try:
        for ln in (bot_dir(b) / "balance_history.jsonl").read_text().splitlines():
            try:
                d = json.loads(ln)
                v = float(d.get("sol_balance", 0) or 0)
                ms = _race_iso_ms(d.get("ts", ""))
                if ms and v > 0 and (since_ts is None or ms >= since_ts):
                    pts.append([ms, round(v, 5)])
            except Exception:
                pass
    except Exception:
        pass
    if len(pts) > max_pts:                 # downsample, always keep the last point
        stride = len(pts) / float(max_pts)
        pts = [pts[int(i * stride)] for i in range(max_pts)] + [pts[-1]]
    return pts


def _race_gate():
    n = ghosts = 0
    net = 0.0
    for b in RACE_BOTS:
        try:
            c = sqlite3.connect(str(bot_dir(b) / "trades.db"))
            for (data,) in c.execute(
                    "SELECT data FROM trades WHERE event='close' ORDER BY rowid DESC LIMIT 500"):
                d = json.loads(data)
                play = d.get("insane_tier") or d.get("play") or d.get("tier")
                if play in RACE_DP_PLAYS:
                    n += 1
                    net += d.get("pnl_sol") or 0.0
                    if d.get("ghost") or (abs(d.get("pnl", 0.0)) < 1e-9 and (d.get("pnl_sol", 0.0) < -0.02)):
                        ghosts += 1
            c.close()
        except Exception:
            pass
    gr = (ghosts / n) if n else 0.0
    return {"passed": (net >= 0) and (gr <= RACE_GHOST_MAX) and (n >= RACE_MIN_CLOSES),
            "n": n, "net": round(net, 5), "ghosts": ghosts, "ghost_rate": round(gr, 4),
            "min_closes": RACE_MIN_CLOSES, "ghost_max": RACE_GHOST_MAX}


def _race_brain():
    """The brain's strongest validated edges (orbs) — what the bots believe right now."""
    try:
        j  = json.loads(Path("shared_memory/strategy_brain.json").read_text())
        le = j.get("last_eval", {}) or {}
        cands = le.get("candidates", {}) or {}
        rows = [{"name": n, "ev_lo": c.get("ev_lo"), "ev": c.get("ev"),
                 "wr": c.get("wr"), "n": c.get("n")}
                for n, c in cands.items() if isinstance(c, dict) and c.get("ev_lo") is not None]
        rows.sort(key=lambda r: r["ev_lo"], reverse=True)
        return {"span_hrs": round(le.get("span_hrs", 0.0) or 0.0, 1), "top": rows[:4]}
    except Exception:
        return {"span_hrs": 0.0, "top": []}


def _race_intent():
    """The latent SIZE lever: what fraction of wallet the edge WOULD take if the gene were
    expressing (full), vs the ~1.5% C² size it uses dormant. Shows the lever waiting."""
    try:
        import ev_sizing
        for rule in ("deep_pool_filling", "deep_pool_strict_filling", "deep_pool"):
            fr = ev_sizing.ev_size_fraction_of_cap(rule)
            if fr is not None:
                return {"rule": rule, "frac_of_cap": round(fr, 3),
                        "wallet_pct_full": round(fr * 0.15 * 0.55 * 100, 1)}  # ×cap ×normal vol_scale
    except Exception:
        pass
    return {"rule": None, "frac_of_cap": None, "wallet_pct_full": None}


def _race_recent(b, limit=6):
    """Last closes for bot b — the decision log, growing as trades commence."""
    out = []
    try:
        c = sqlite3.connect(str(bot_dir(b) / "trades.db"))
        for (data,) in c.execute(
                "SELECT data FROM trades WHERE event='close' ORDER BY rowid DESC LIMIT ?", (limit,)):
            d = json.loads(data)
            play = d.get("insane_tier") or d.get("play") or d.get("tier") or "?"
            ghost = bool(d.get("ghost") or (abs(d.get("pnl", 0.0)) < 1e-9 and (d.get("pnl_sol", 0.0) < -0.02)))
            out.append({"t": str(d.get("ts", ""))[11:16], "play": play,
                        "sym": (d.get("symbol") or d.get("sym") or str(d.get("mint", ""))[:4]),
                        "pnl_sol": d.get("pnl_sol"), "ghost": ghost, "dp": play in RACE_DP_PLAYS})
        c.close()
    except Exception:
        pass
    return out


def _race_open(b):
    """Open positions for bot b — what it's doing right now."""
    out = []
    try:
        s = json.loads((bot_dir(b) / "status.json").read_text())
        for mint, p in (s.get("open_positions", {}) or {}).items():
            sl = p.get("sl_floor_pct")
            out.append({"sym": str(mint)[:4], "play": p.get("insane_tier") or p.get("play") or "?",
                        "size_sol": p.get("size_sol"), "tp1": bool(p.get("tp1_taken")),
                        "be": (sl is not None and (sl or -1) >= 0)})
    except Exception:
        pass
    return out


# ── S126: per-bot IDENTITY + EXIT-MIX for the GENE tab ────────────────────────────────
# The SIZE gene is dormant (correct — kill_criterion FAIL), so the GENE tab read as
# identical/empty across bots. These read-only helpers surface the differentiation that
# exists TODAY: mode, S124 size-tier, the admit_guard lane allowlist + RUNNEREDGE arm
# state, the exit-family mix, and the runner_watch verdict. Additive fields only; every
# read is exception-wrapped so a missing/corrupt file can never 500 /api/race.
_S126_EXIT_FAMILIES = ("trail", "tp", "stop", "dead", "rug", "ghost", "time", "other")


def _race_identity(b):
    out = {"mode": None, "size_tier": None, "lane_mode": None, "lanes": [],
           "runner_armed": False}
    try:
        s = json.loads((bot_dir(b) / "status.json").read_text())
        out["mode"] = s.get("mode")
        out["size_tier"] = s.get("size_tier")
    except Exception:
        pass
    try:
        ag = json.loads((bot_dir(b) / "admit_guard.json").read_text())
        if ag.get("enabled"):
            out["lane_mode"] = ag.get("mode")
            out["lanes"] = ag.get("lanes") or []
    except Exception:
        pass
    try:
        mo = json.loads((bot_dir(b) / "momentum_override.json").read_text())
        out["runner_armed"] = bool(mo.get("enabled"))
    except Exception:
        pass
    return out


def _race_exit_mix(b, limit=100):
    """Exit-family split + decisive win-rate over the last `limit` closes. WR judged on
    return-% (pnl_sol/size_sol) with a ±1% neutral band — the S89 percent-not-dollars rule."""
    fam = {k: 0 for k in _S126_EXIT_FAMILIES}
    wins = losses = neutral = 0
    try:
        c = sqlite3.connect(str(bot_dir(b) / "trades.db"))
        for (data,) in c.execute(
                "SELECT data FROM trades WHERE event='close' ORDER BY rowid DESC LIMIT ?", (limit,)):
            d = json.loads(data)
            r = (d.get("exit_reason") or "").lower()
            if d.get("ghost") or r.startswith("ghost"):
                fam["ghost"] += 1
            elif r.startswith("smart trail"):
                fam["trail"] += 1
            elif r.startswith("tp bank") or r.startswith("take profit"):
                fam["tp"] += 1
            elif r.startswith("stop loss"):
                fam["stop"] += 1
            elif r.startswith("catastrophic"):
                fam["rug"] += 1
            elif r.startswith("dead pool"):
                fam["dead"] += 1
            elif r.startswith("time limit"):
                fam["time"] += 1
            else:
                fam["other"] += 1
            try:
                ret = 100.0 * float(d.get("pnl_sol") or 0.0) / float(d.get("size_sol"))
            except Exception:
                ret = 0.0
            if ret > 1.0:
                wins += 1
            elif ret < -1.0:
                losses += 1
            else:
                neutral += 1
        c.close()
    except Exception:
        pass
    n = wins + losses
    return {"families": fam, "closes": sum(fam.values()),
            "wins": wins, "losses": losses, "neutral": neutral,
            "wr": round(100.0 * wins / n, 1) if n else None}


def _race_runner_watch():
    """Last runner_watch verdict (the RUNNEREDGE pre-registered judge) — read-only."""
    try:
        last = Path("runner_watch_log.jsonl").read_text().strip().splitlines()[-1]
        d = json.loads(last)
        return {"verdict": d.get("verdict"), "n": d.get("n_sized"),
                "target": d.get("n_threshold"), "binding": (d.get("binding") or "")[:90],
                "ts": d.get("ts")}
    except Exception:
        return None


@app.get("/api/race")
async def get_race(request: Request):
    """Gen-1 gene-pool race: per-bot trajectory, gene, edges, decisions, and the deploy gate."""
    _check_get_rate(request)
    start = {}
    try:
        start = json.loads(Path("race_start.json").read_text())
    except Exception:
        pass
    started_at = start.get("started_at", "")
    since_ts   = _race_iso_ms(started_at)
    start_tot  = start.get("start_total", {})
    bots = []
    for b in RACE_BOTS:
        gene, gene_short, armed, gscale, ggen, gparent = _race_gene(b)
        total = _race_total(b)
        st0   = float(start_tot.get(str(b), total) or total)
        bots.append({
            "bot": b, "color": RACE_COLORS.get(b, "#00ff41"),
            "gene": gene, "gene_short": gene_short, "armed": armed,
            "scale": gscale, "gen": ggen, "parent": gparent,
            "total": round(total, 5), "start": round(st0, 5), "delta": round(total - st0, 5),
            "pct": round(100.0 * total / RACE_GOAL, 1),
            "series": _race_series(b, since_ts),
            "recent": _race_recent(b), "open": _race_open(b),
            "ident": _race_identity(b), "exits": _race_exit_mix(b),   # S126
        })
    for i, x in enumerate(sorted(bots, key=lambda z: z["total"], reverse=True), 1):
        x["rank"] = i
    elapsed_h = ((time.time() * 1000.0 - since_ts) / 3600000.0) if since_ts else 0.0
    return JSONResponse({"goal": RACE_GOAL, "started_at": started_at,
                         "elapsed_h": round(elapsed_h, 2), "gate": _race_gate(),
                         "brain": _race_brain(), "intent": _race_intent(), "bots": bots,
                         "runner_watch": _race_runner_watch()})   # S126


@app.get("/api/regime_ev")
async def get_regime_ev(request: Request):
    """S84: per-REGIME × per-PLAY realized edge (clean, ghost-free). The evidence layer for
    regime-conditional admission + (later) per-regime SIZE genes. Read-only; feeds the GENE
    tab's regime-edge matrix. Exception-wrapped so a bad row can never 500 the dashboard."""
    _check_get_rate(request)
    try:
        import regime_ev as _rev
        return JSONResponse(_rev.payload(all_time=False))
    except Exception as e:
        return JSONResponse({"plays": [], "regimes": [], "cells": [], "regime_totals": [],
                             "error": str(e)[:120]}, status_code=200)


def _signal_feed() -> dict:
    """S97: score the live discovery feed for the day's highest-conviction Jotchua-shape and
    promote it into a HARD-CAPPED daily list (≤_SIGNAL_DAILY_CAP, score ≥_SIGNAL_RUNNER_MIN).

    Read-mostly: scores the public sidecar snapshot via virality_probe, then maintains a tiny
    dedup/cap state file so the banner stays "no noise" (1–3/day). Dedups by mint within the
    UTC day; resets at the day boundary. Every write is best-effort (a failure never breaks the
    read). Returns {signals:[…newest first…], cap, runner_min, feed_ts, today}.
    """
    import virality_probe as _vp
    from datetime import timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # load + roll the daily state
    state = {"date": today, "signals": []}
    try:
        prev = json.loads(_SIGNAL_STATE.read_text())
        if prev.get("date") == today:
            state = prev
    except Exception:
        pass
    have = {s.get("mint") for s in state["signals"]}

    # score the live feed (same dict shape virality_probe.scan consumes)
    feed_ts, best = None, None
    try:
        snap = json.loads((Path("shared_memory") / "discovery_snapshot.json").read_text())
        feed_ts = snap.get("ts")
        md = snap.get("market_data", {}) or {}
        lv = snap.get("liq_velocity", {}) or {}
        for mint, m in md.items():
            if mint in have:
                continue
            r = _vp.score(m, liq_vel=lv.get(mint))
            if r["score"] < _SIGNAL_RUNNER_MIN:
                continue
            cand = {
                "mint": mint, "symbol": m.get("symbol", "?"), "score": r["score"],
                "verdict": r["verdict"], "vacc": r["vacc"], "bp1h": r["bp1h"],
                "liq": round(m.get("liquidity_usd", 0) or 0),
                "c1h": m.get("price_change_1h", 0) or 0,
                "c6h": m.get("price_change_6h", 0) or 0,
                "ts": time.time(),
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    except Exception:
        pass

    # promote the single best NEW runner if there's room under the daily cap
    if best is not None and len(state["signals"]) < _SIGNAL_DAILY_CAP:
        state["signals"].append(best)
        try:
            _SIGNAL_STATE.write_text(json.dumps(state))
        except Exception:
            pass

    sig = sorted(state["signals"], key=lambda s: s.get("ts", 0), reverse=True)
    return {"signals": sig, "cap": _SIGNAL_DAILY_CAP,
            "runner_min": _SIGNAL_RUNNER_MIN, "feed_ts": feed_ts, "today": today}


@app.get("/api/signals")
async def get_signals(request: Request):
    """S97: ADMIN-ONLY live signal banner — the day's top Jotchua-shaped candidates.

    Hard-gated to admin (NOT sanitized like the live endpoints) for the same reason as
    /api/agents: surfacing the fleet's highest-conviction candidate entries is strategy-
    inference / front-running risk if shown to public spectators. Exception-wrapped so a bad
    feed can never 500 the dashboard."""
    if not _is_authed(request):
        raise HTTPException(status_code=403, detail="admin only")
    try:
        return JSONResponse(_signal_feed())
    except Exception as e:
        return JSONResponse({"signals": [], "cap": _SIGNAL_DAILY_CAP,
                             "runner_min": _SIGNAL_RUNNER_MIN, "error": str(e)[:120]},
                            status_code=200)


@app.get("/api/agents")
async def get_agents(request: Request):
    """ADMIN-ONLY: reasoning-agent briefs (Strategy Researcher + Execution Engineer).

    These are internal strategy intelligence — candidate edges, leak diagnoses, proposed
    fixes. Never served to public spectators (front-running / strategy-inference risk), so
    this endpoint hard-requires auth rather than sanitizing like the live endpoints do.
    """
    if not _is_authed(request):
        raise HTTPException(status_code=403, detail="admin only")

    def _load(p):
        try:
            return json.loads(Path(p).read_text())
        except Exception:
            return None

    briefs = {"strategy":  _load("shared_memory/agent_strategy.json"),
              "execution": _load("shared_memory/agent_execution.json")}
    # rolling cost ledger — last 24h spend (answers "what is this costing")
    cost_24h, runs_24h, last = 0.0, 0, None
    try:
        day0 = time.time() - 86400
        for line in Path("agents/history/token_usage.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            last = r
            if r.get("ts", 0) >= day0:
                cost_24h += float(r.get("cost_usd") or 0.0)
                runs_24h += 1
    except Exception:
        pass
    return JSONResponse({"briefs": briefs,
                         "cost": {"usd_24h": round(cost_24h, 4), "runs_24h": runs_24h, "last": last}})


# ── Per-bot data endpoints ────────────────────────────────────────────────────

@app.get("/api/bots/{bot_id}/status")
async def get_bot_status(bot_id: int, request: Request):
    _check_get_rate(request)
    _validate_bot(bot_id)
    sf = bot_dir(bot_id) / "status.json"
    if not sf.exists():
        return JSONResponse({"error": "Bot not running"})
    return JSONResponse(_sanitize_live(json.loads(sf.read_text()), _is_authed(request)))


@app.get("/api/bots/{bot_id}/trades")
async def get_bot_trades(bot_id: int, request: Request, limit: int = 100):
    _check_get_rate(request)
    _validate_bot(bot_id)
    limit = min(limit, 500)
    db_path = bot_dir(bot_id) / "trades.db"
    if not db_path.exists():
        return JSONResponse([])
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # The LEDGER filters this payload to close events client-side. A bot that's
        # been rejecting tokens for a while (e.g. bot1's high reject volume) pushes
        # its sparse closes out of a flat "last N rows" window, leaving the ledger
        # empty even though closed trades exist. Guarantee the most-recent closes
        # are always carried alongside the recent activity-log rows. (S84)
        rows = conn.execute(
            """SELECT data FROM trades WHERE id IN (
                   SELECT id FROM (SELECT id FROM trades ORDER BY id DESC LIMIT ?)
                   UNION
                   SELECT id FROM (
                       SELECT id FROM trades
                       WHERE event IN ('close','partial_close')
                       ORDER BY id DESC LIMIT 40
                   )
               ) ORDER BY id DESC""",
            (limit,),
        ).fetchall()
        conn.close()
        return JSONResponse([json.loads(r[0]) for r in rows])
    except Exception:
        return JSONResponse([])


@app.get("/api/bots/{bot_id}/history")
async def get_bot_history(bot_id: int, request: Request, points: int = 120):
    _check_get_rate(request)
    _validate_bot(bot_id)
    points = min(points, 2880)
    hp = bot_dir(bot_id) / "balance_history.jsonl"
    if not hp.exists():
        return JSONResponse([])
    lines = hp.read_text().strip().splitlines()
    return JSONResponse([json.loads(l) for l in lines if l][-points:])


@app.get("/api/bots/{bot_id}/balance")
async def get_bot_balance(bot_id: int, request: Request):
    _check_get_rate(request)
    _validate_bot(bot_id)
    bot = fleet_entry(bot_id)
    if not bot or not bot.get("pubkey"):
        return JSONResponse({"sol": 0, "funded": False})
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(HELIUS_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance", "params": [bot["pubkey"]],
            }, timeout=10)
            sol = r.json().get("result", {}).get("value", 0) / 1e9
        return JSONResponse({"sol": round(sol, 4), "funded": sol >= ACTIVATION_MIN_SOL})
    except Exception:
        return JSONResponse({"sol": 0, "funded": False})


# ── Activation ────────────────────────────────────────────────────────────────

@app.post("/api/bots/{bot_id}/activate")
async def activate_bot(bot_id: int, request: Request):
    _require_auth(request)
    if bot_id not in BOT_IDS:
        return JSONResponse({"ok": False, "error": "Invalid bot ID"})
    fleet = load_fleet()
    bot = next((b for b in fleet["bots"] if b["id"] == bot_id), None)
    if not bot:
        return JSONResponse({"ok": False, "error": "Bot not found"})
    if bot["status"] == "row2_locked":
        return JSONResponse({"ok": False, "error": "Row 2 locked — achieve ◎2.0 prestige to unlock"})
    if bot["status"] != "undeployed":
        return JSONResponse({"ok": False, "error": f"Bot is already {bot['status']}"})

    keypair = Keypair()
    data_dir = bot_dir(bot_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    kp_path = data_dir / "keypair.json"
    # Write keypair atomically — a partial write of a private key is unrecoverable.
    _kp_tmp = kp_path.with_suffix(".tmp")
    _kp_tmp.write_text(json.dumps(list(bytes(keypair))))
    os.replace(_kp_tmp, kp_path)
    kp_path.chmod(0o600)   # owner-read-only: private key should never be world-readable
    # Write defaults: Stoic + High cap + Medium size
    (data_dir / "trading_mode.json").write_text(json.dumps({"mode": "stoic"}))
    (data_dir / "marketcap_mode.json").write_text(json.dumps({"marketcap": "high"}))
    (data_dir / "size_mode.json").write_text(json.dumps({"size_tier": "medium"}))

    pubkey = str(keypair.pubkey())
    bot["status"]       = "pending_activation"
    bot["pubkey"]       = pubkey
    bot["keypair_path"] = str(kp_path)
    save_fleet(fleet)

    return JSONResponse({"ok": True, "pubkey": pubkey, "min_sol": ACTIVATION_MIN_SOL})


# ── Bot controls ──────────────────────────────────────────────────────────────

@app.post("/api/bots/{bot_id}/set-mode")
async def set_bot_mode(bot_id: int, mode: str, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    if mode not in ("wild", "stoic", "insane"):
        return JSONResponse({"ok": False, "error": "Invalid mode"})
    (bot_dir(bot_id) / "trading_mode.json").write_text(json.dumps({"mode": mode}))
    return JSONResponse({"ok": True, "mode": mode})


@app.post("/api/bots/{bot_id}/set-marketcap")
async def set_bot_marketcap(bot_id: int, cap: str, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    if cap not in ("low", "mid", "high"):
        return JSONResponse({"ok": False, "error": "Invalid cap"})
    (bot_dir(bot_id) / "marketcap_mode.json").write_text(json.dumps({"marketcap": cap}))
    return JSONResponse({"ok": True, "marketcap": cap})


@app.post("/api/bots/{bot_id}/set-size-tier")
async def set_bot_size_tier(bot_id: int, tier: str, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    if tier not in ("small", "medium", "large"):
        return JSONResponse({"ok": False, "error": "Invalid tier"})
    (bot_dir(bot_id) / "size_mode.json").write_text(json.dumps({"size_tier": tier}))
    return JSONResponse({"ok": True, "size_tier": tier})


@app.post("/api/bots/{bot_id}/set-viral-weight")
async def set_bot_viral_weight(bot_id: int, weight: str, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    if weight not in ("off", "normal", "boost"):
        return JSONResponse({"ok": False, "error": "Invalid weight"})
    (bot_dir(bot_id) / "viral_weight.json").write_text(json.dumps({"viral_weight": weight}))
    return JSONResponse({"ok": True, "viral_weight": weight})


@app.post("/api/bots/{bot_id}/exit-all")
async def bot_exit_all(bot_id: int, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    (bot_dir(bot_id) / "exit_requested.json").write_text("{}")
    return JSONResponse({"ok": True})


@app.post("/api/bots/{bot_id}/pause")
async def pause_bot(bot_id: int, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    d = bot_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "exit_requested.json").write_text("{}")  # close all open positions
    (d / "pause_flag.json").write_text("{}")       # prevent new entries
    return JSONResponse({"ok": True})


@app.post("/api/bots/{bot_id}/resume")
async def resume_bot(bot_id: int, request: Request):
    _require_auth(request); _validate_bot(bot_id)
    status_path = bot_dir(bot_id) / "status.json"
    if status_path.exists():
        try:
            st = json.loads(status_path.read_text())
            bal = st.get("sol_balance", 0)
            if bal < ACTIVATION_MIN_SOL:
                return JSONResponse({"ok": False, "error": f"Insufficient balance ◎{bal:.4f} — send ◎{ACTIVATION_MIN_SOL}+ SOL to activate"})
        except Exception:
            pass
    pf = bot_dir(bot_id) / "pause_flag.json"
    if pf.exists():
        pf.unlink()
    return JSONResponse({"ok": True})


@app.post("/api/bots/{bot_id}/test-payout")
async def bot_test_payout(bot_id: int, request: Request, amount: float = 0.01):
    _require_auth(request); _validate_bot(bot_id)
    amount = min(max(amount, 0.001), 0.05)   # clamp: 0.001–0.05 SOL
    bot = fleet_entry(bot_id)
    if not bot or not bot.get("keypair_path"):
        return JSONResponse({"ok": False, "error": "Bot not configured"})
    pw_file = bot_dir(bot_id) / "payout_wallet.json"
    if not pw_file.exists():
        return JSONResponse({"ok": False, "error": "No payout wallet locked yet"})
    dest = json.loads(pw_file.read_text()).get("wallet")
    if not dest:
        return JSONResponse({"ok": False, "error": "No payout wallet locked yet"})
    try:
        from payout import send_sol, get_sol_balance
        keypair = _load_keypair(bot["keypair_path"])
        async with httpx.AsyncClient() as client:
            sol_bal = await get_sol_balance(client, str(keypair.pubkey()))
            if sol_bal < amount + 0.005:
                return JSONResponse({"ok": False, "error": f"Insufficient SOL: ◎{sol_bal:.4f}"})
            sig = await send_sol(client, keypair, dest, amount)
            if sig:
                _audit_write(bot_id, {"event": "payout", "milestone": "test",
                                       "label": f"Test payout ◎{amount}",
                                       "amount_sol": amount, "to": dest, "sig": sig})
                return JSONResponse({"ok": True, "sig": sig, "amount": amount, "to": dest})
        return JSONResponse({"ok": False, "error": "Transaction failed"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "Internal error"})


@app.get("/api/bots/{bot_id}/drain-preview")
async def drain_preview(bot_id: int, request: Request):
    """Return drain details before confirming: exact amount, destination, current balance."""
    _require_auth(request); _validate_bot(bot_id)
    bot = fleet_entry(bot_id)
    if not bot or not bot.get("keypair_path"):
        return JSONResponse({"ok": False, "error": "Bot not configured"})
    pw_file = bot_dir(bot_id) / "payout_wallet.json"
    if not pw_file.exists():
        return JSONResponse({"ok": False, "error": "No funding wallet locked"})
    dest = json.loads(pw_file.read_text()).get("wallet","")
    if not dest:
        return JSONResponse({"ok": False, "error": "No funding wallet address found"})
    GAS_RESERVE = 0.005
    try:
        from payout import get_sol_balance
        keypair = _load_keypair(bot["keypair_path"])
        async with httpx.AsyncClient() as client:
            sol_bal = await get_sol_balance(client, str(keypair.pubkey()))
        drain_amount = max(0.0, round(sol_bal - GAS_RESERVE, 6))
        return JSONResponse({
            "ok": True,
            "balance": sol_bal,
            "drain_amount": drain_amount,
            "gas_reserve": GAS_RESERVE,
            "destination": dest,
            "can_drain": drain_amount > 0,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": "Internal error"})


@app.post("/api/bots/{bot_id}/drain")
async def drain_bot(bot_id: int, request: Request):
    """Send all available SOL back to the locked funding wallet. Bot must be paused."""
    _require_auth(request); _validate_bot(bot_id)
    bot = fleet_entry(bot_id)
    if not bot or not bot.get("keypair_path"):
        return JSONResponse({"ok": False, "error": "Bot not configured"})

    pause_path = bot_dir(bot_id) / "pause_flag.json"
    if not pause_path.exists():
        return JSONResponse({"ok": False, "error": "Bot must be paused before draining"})

    pw_file = bot_dir(bot_id) / "payout_wallet.json"
    if not pw_file.exists():
        return JSONResponse({"ok": False, "error": "No funding wallet locked — cannot drain"})
    dest = json.loads(pw_file.read_text()).get("wallet")
    if not dest:
        return JSONResponse({"ok": False, "error": "No funding wallet address found"})

    GAS_RESERVE = 0.005  # keep just enough for the transfer fee
    try:
        from payout import send_sol, get_sol_balance
        keypair = _load_keypair(bot["keypair_path"])
        async with httpx.AsyncClient() as client:
            sol_bal = await get_sol_balance(client, str(keypair.pubkey()))
            drain_amount = round(sol_bal - GAS_RESERVE, 6)
            if drain_amount <= 0:
                return JSONResponse({"ok": False, "error": f"Insufficient balance: ◎{sol_bal:.4f}"})
            sig = await send_sol(client, keypair, dest, drain_amount)
            if sig:
                _audit_write(bot_id, {
                    "event": "drain",
                    "amount_sol": drain_amount,
                    "to": dest,
                    "sig": sig,
                    "balance_before": sol_bal,
                })
                # Clear balance history so the chart starts fresh on next funding
                history_path = bot_dir(bot_id) / "balance_history.jsonl"
                if history_path.exists():
                    history_path.write_text("")
                # Clear activity log (trades.db) — keep only the drain event just written
                db_path = bot_dir(bot_id) / "trades.db"
                if db_path.exists():
                    try:
                        conn = sqlite3.connect(str(db_path))
                        conn.execute("DELETE FROM trades WHERE id NOT IN (SELECT MAX(id) FROM trades)")
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
                # Clear pause flag so bot starts running immediately on next funding
                if pause_path.exists():
                    pause_path.unlink()
                return JSONResponse({
                    "ok": True, "sig": sig,
                    "amount": drain_amount, "to": dest,
                    "balance_before": sol_bal,
                })
        return JSONResponse({"ok": False, "error": "Transaction failed"})
    except Exception as e:
        print(f"[Fleet] Drain error (bot {bot_id}): {e}")
        return JSONResponse({"ok": False, "error": "Internal error"})


# ── Legacy compat (bot 1) ─────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status(request: Request):
    return await get_bot_status(1, request)

@app.get("/api/trades")
async def get_trades(request: Request, limit: int = 100):
    return await get_bot_trades(1, request, min(limit, 500))

@app.get("/api/history")
async def get_history(request: Request, points: int = 120):
    return await get_bot_history(1, request, min(points, 2880))

@app.post("/api/set-mode")
async def set_mode(mode: str, request: Request):
    return await set_bot_mode(1, mode, request)

@app.post("/api/set-marketcap")
async def set_marketcap(cap: str, request: Request):
    return await set_bot_marketcap(1, cap, request)

@app.post("/api/set-size-tier")
async def set_size_tier(tier: str, request: Request):
    return await set_bot_size_tier(1, tier, request)

@app.post("/api/set-viral-weight")
async def set_viral_weight(weight: str, request: Request):
    return await set_bot_viral_weight(1, weight, request)

@app.post("/api/exit-all")
async def exit_all(request: Request):
    return await bot_exit_all(1, request)

@app.post("/api/pause")
async def pause(request: Request): return await pause_bot(1, request)

@app.post("/api/resume")
async def resume(request: Request): return await resume_bot(1, request)

@app.post("/api/test-payout")
async def test_payout(request: Request, amount: float = 0.01):
    return await bot_test_payout(1, request, amount)

@app.get("/api/balances")
async def get_balances(wallet: str, request: Request):
    # Requires auth — this endpoint proxies arbitrary wallet queries through the Helius key.
    _require_auth(request)
    # Validate wallet looks like a Solana base58 address (32-44 alphanumeric chars)
    import re as _re
    if not _re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', wallet):
        return JSONResponse({"sol": 0, "usdc": 0, "error": "Invalid wallet"})
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(HELIUS_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance", "params": [wallet],
            }, timeout=10)
            sol = r.json().get("result", {}).get("value", 0) / 1e9
        return JSONResponse({"sol": round(sol, 4), "usdc": 0})
    except Exception:
        return JSONResponse({"sol": 0, "usdc": 0, "error": "Internal error"})


if __name__ == "__main__":
    import uvicorn
    # Defaults to localhost-only (safe). Set DASHBOARD_HOST=0.0.0.0 to expose on your
    # network, and DASHBOARD_PORT to change the port (e.g. if 8080 is taken).
    _host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip()
    _port = int(os.getenv("DASHBOARD_PORT", "8080"))
    uvicorn.run("dashboard:app", host=_host, port=_port, reload=False)
