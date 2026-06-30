#!/usr/bin/env python3
"""
PIPBOI — doctor: a friendly self-check that tells you exactly what (if anything)
is wrong and the one command to fix it.

    ./pipboi doctor              # full health check
    ./pipboi doctor --preflight  # only the must-pass-to-boot checks (used by run.sh)

Stdlib-only on purpose — it runs even before `pip install`, since checking whether
your dependencies are installed is one of the things it does.
"""
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # src/
REPO = ROOT.parent                               # repo root
ENV = REPO / ".env"

# colors (no-op if not a tty)
_tty = sys.stdout.isatty()
G = "\033[1;32m" if _tty else ""
Y = "\033[1;33m" if _tty else ""
Rd = "\033[1;31m" if _tty else ""
D = "\033[2;32m" if _tty else ""
B = "\033[1m" if _tty else ""
R = "\033[0m" if _tty else ""

OK, WARN, BAD = f"{G}✅{R}", f"{Y}⚠ {R}", f"{Rd}❌{R}"

REQUIRED_DEPS = ["httpx", "solders", "dotenv", "fastapi", "uvicorn", "websockets"]
DEP_PKG = {"dotenv": "python-dotenv"}   # import name → pip name where they differ


def _read_env():
    cfg = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def _port_busy(port=8080):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def check_python():
    v = sys.version_info
    if v >= (3, 9):
        return OK, f"Python {v.major}.{v.minor}.{v.micro}", None
    return BAD, f"Python {v.major}.{v.minor} is too old", \
        "PIPBOI needs Python 3.9+. Install a newer Python (e.g. python.org or `brew install python`)."


def check_venv():
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        return OK, "virtual environment active", None
    return WARN, "no virtualenv active (optional but recommended)", \
        "python3 -m venv .venv && source .venv/bin/activate"


def check_deps():
    missing = []
    for mod in REQUIRED_DEPS:
        try:
            __import__(mod)
        except Exception:
            missing.append(DEP_PKG.get(mod, mod))
    if not missing:
        return OK, "all dependencies installed", None
    return BAD, f"missing: {', '.join(missing)}", "pip install -r requirements.txt"


def check_env():
    if ENV.exists():
        return OK, ".env present (at repo root)", None
    return WARN, "no .env yet (created automatically on first run)", \
        "./pipboi setup   (or just ./pipboi run and finish in the browser)"


def check_helius(cfg):
    key = cfg.get("HELIUS_API_KEY", "")
    if key and key != "your_helius_api_key_here":
        return OK, "Helius API key is set", None
    return WARN, "Helius API key not set yet", \
        "Get a free key at https://dashboard.helius.dev → enter it at http://localhost:8080 (or ./pipboi setup)"


def check_pin(cfg):
    pin = cfg.get("DASHBOARD_PIN", "")
    if pin:
        return OK, f"dashboard PIN: {B}{pin}{R}  (enter this at localhost:8080 to unlock admin)", None
    return WARN, "no dashboard PIN yet (generated on first run)", None


def check_wallet(cfg):
    kp = cfg.get("KEYPAIR_PATH", "")
    if not kp:
        return WARN, "no wallet configured yet", \
            "Click Activate in the dashboard — it generates one and shows the address to fund."
    p = Path(os.path.expanduser(kp))
    if p.exists():
        return OK, f"wallet keypair found ({kp})", None
    return WARN, f"wallet keypair not created yet ({kp})", \
        "Click Activate in the dashboard to generate + fund it (or bring your own keypair at this path)."


def check_port():
    if _port_busy():
        return WARN, "port 8080 is in use — PIPBOI may already be running", \
            "Open http://localhost:8080 . If that's something else, stop it or set DASHBOARD_PORT in .env."
    return OK, "port 8080 is free", None


def check_running():
    try:
        import subprocess
        out = subprocess.run(["pgrep", "-f", "dashboard.py"], capture_output=True, text=True)
        if out.stdout.strip():
            return OK, "dashboard process is running", None
    except Exception:
        pass
    return WARN, "bot not running right now", "./pipboi run"


def preflight():
    """The two checks that MUST pass before the dashboard can boot. Returns 0/1."""
    bad = []
    for fn in (check_python, check_deps):
        sym, msg, fix = fn()
        if sym == BAD:
            bad.append((msg, fix))
    if bad:
        print(f"\n{Rd}{B}PIPBOI can't start yet:{R}")
        for msg, fix in bad:
            print(f"  {BAD} {msg}")
            if fix:
                print(f"     {D}→ fix:{R} {B}{fix}{R}")
        print()
        return 1
    return 0


def main():
    print(f"\n{G}{B}🐸  PIPBOI doctor{R}  {D}— checking your setup{R}\n")
    cfg = _read_env()
    # (check, optional?) — optional checks never become the headline "next step"
    checks = [
        (check_python(), False),
        (check_venv(), True),
        (check_deps(), False),
        (check_env(), False),
        (check_helius(cfg), False),
        (check_pin(cfg), True),
        (check_wallet(cfg), True),
        (check_port(), True),
        (check_running(), False),
    ]
    n_ok = sum(1 for (sym, _, _), _ in checks if sym == OK)
    bad_fix = warn_fix = None
    for (sym, msg, fix), optional in checks:
        print(f"  {sym} {msg}")
        if fix:
            print(f"      {D}→{R} {fix}")
        if fix and not optional:
            if sym == BAD and bad_fix is None:
                bad_fix = fix
            elif sym == WARN and warn_fix is None:
                warn_fix = fix
    first_fix = bad_fix or warn_fix

    print(f"\n{D}{n_ok}/{len(checks)} green.{R}")
    if first_fix:
        print(f"{B}Next step:{R}  {G}{first_fix}{R}\n")
    else:
        print(f"{G}{B}All good — you're ready to trade. Watch it at http://localhost:8080{R}\n")
    return 0


if __name__ == "__main__":
    if "--preflight" in sys.argv:
        sys.exit(preflight())
    sys.exit(main())
