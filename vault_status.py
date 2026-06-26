#!/usr/bin/env python3
"""
Dat Pip Boi — terminal status viewer.

The simplest way to check on your bot: just ask Claude "how's my bot doing?"
and it runs this. No web server, no browser, no localhost — a clean readout
right here in the terminal.

  python3 vault_status.py            # bot 1 (default)
  python3 vault_status.py --bot 2    # a specific bot
  python3 vault_status.py --all      # every bot you're running
  python3 vault_status.py --trades 8 # show more recent trades

Read-only. Touches nothing the bot owns — it only reads status.json + the
trade ledger. Safe to run any time, while the bot is trading or stopped.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRESTIGE_GOAL = 2.0  # ◎ — a bot "graduates" (prestiges) when it doubles to here

# ── phosphor palette (degrades to plain text when not a TTY / NO_COLOR set) ──
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str) -> str:
    return code if _TTY else ""
GREEN  = _c("\033[38;5;48m")   # vivid phosphor green (Dat Pip Boi)
DIM    = _c("\033[38;5;65m")   # muted green
CORAL  = _c("\033[38;5;210m")  # soft loss coral (never alarm-red)
WHITE  = _c("\033[38;5;255m")
GREY   = _c("\033[38;5;245m")
BOLD   = _c("\033[1m")
RST    = _c("\033[0m")

W = 60  # inner card width


def _pnl_color(v: float) -> str:
    if v > 1e-9:
        return GREEN
    if v < -1e-9:
        return CORAL
    return GREY


def _exit_why(reason: str, hold_min) -> str:
    """Collapse the verbose internal exit_reason into a short readable tag.
    Mirrors the dashboard's S112 _exitWhy so the terminal reads the same."""
    r = (reason or "").lower()
    tag = reason or "—"
    if "smart trail" in r or r.startswith("trail"):
        pct = _first_pct(reason)
        tag = f"trail {pct}" if pct else "trail"
    elif "stop loss" in r or r.startswith("stop"):
        if "[be floor]" in r or "break" in r:
            tag = "break-even"
        else:
            pct = _first_pct(reason)
            tag = f"stop {pct}" if pct else "stop"
    elif "tp bank" in r or "take profit" in r or "take-profit" in r:
        pct = _first_pct(reason)
        tag = f"take-profit {pct}" if pct else "take-profit"
    elif "catastrophic" in r or "lp drain" in r or "rug" in r:
        tag = "rug exit"
    elif "dead pool" in r:
        tag = "dead pool"
    elif "time limit" in r or "max hold" in r:
        tag = "time limit"
    elif "ghost" in r:
        tag = "ghost"
    # hold time
    h = ""
    try:
        m = float(hold_min)
        h = f"{m:.0f}m" if m < 90 else f"{m/60:.1f}h"
    except (TypeError, ValueError):
        pass
    return f"{tag:<13}{h}"


def _first_pct(s: str):
    """First +N%/-N% token in a string, e.g. 'Smart trail +31.1% (peak...' -> '+31%'."""
    import re
    if not s:
        return ""
    m = re.search(r"([+-]?\d+(?:\.\d+)?)%", s)
    if not m:
        return ""
    try:
        v = float(m.group(1))
        if abs(v) > 1000:   # phantom-glitch guard (never print +485538%)
            return ""
        return f"{v:+.0f}%"
    except ValueError:
        return ""


def _bar(frac: float, width: int = 34) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    return GREEN + "█" * fill + DIM + "░" * (width - fill) + RST


def _line(text: str = "", pad_visible: int = None) -> str:
    """One card row. pad_visible = the *visible* length (ANSI-stripped) for padding."""
    if pad_visible is None:
        pad_visible = _visible_len(text)
    gap = max(0, W - pad_visible)
    return f"{DIM}║{RST}  {text}{' ' * gap}{DIM}║{RST}"


def _visible_len(s: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def _read_status(bot: int):
    p = ROOT / "bots" / f"bot{bot}" / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _recent_closes(bot: int, n: int):
    db = ROOT / "bots" / f"bot{bot}" / "trades.db"
    if not db.exists():
        return [], 0.0
    rows, net = [], 0.0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.execute("SELECT data FROM trades WHERE event='close' ORDER BY rowid DESC")
        for (data,) in cur:
            try:
                d = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            pnl = d.get("pnl_sol", 0.0) or 0.0
            if abs(pnl) > 0.5:   # skip any uncorrected phantom row
                continue
            net += pnl
            if len(rows) < n:
                rows.append(d)
        con.close()
    except sqlite3.Error:
        return rows, net
    return rows, net


def render_bot(bot: int, n_trades: int = 5) -> str:
    st = _read_status(bot)
    out = []
    top = f"{DIM}╔{'═' * (W + 2)}╗{RST}"
    mid = f"{DIM}╠{'═' * (W + 2)}╣{RST}"
    _suffix = f" · bot {bot}" if bot != 1 else ""
    bot_lbl = f"{BOLD}{GREEN}DAT PIP BOI{RST}{DIM}{_suffix}{RST}"

    if st is None:
        out.append(top)
        out.append(_line(bot_lbl, _visible_len(bot_lbl)))
        out.append(mid)
        out.append(_line(f"{WHITE}No bot running yet — three steps to start:{RST}",
                         _visible_len("No bot running yet — three steps to start:")))
        out.append(_line())
        for txt, plain in (
            (f"{GREEN}1.{RST} {WHITE}./run.sh{RST}            {GREY}start the bot + dashboard{RST}",
             "1. ./run.sh            start the bot + dashboard"),
            (f"{GREEN}2.{RST} open {WHITE}localhost:8080{RST}  {GREY}→ click Activate, fund the wallet{RST}",
             "2. open localhost:8080  → click Activate, fund the wallet"),
            (f"{GREEN}3.{RST} {GREY}ask me {RST}{WHITE}“how's my bot?”{RST}{GREY} again once it trades{RST}",
             "3. ask me “how's my bot?” again once it trades"),
        ):
            out.append(_line(txt, _visible_len(plain)))
        out.append(f"{DIM}╚{'═' * (W + 2)}╝{RST}")
        return "\n".join(out)

    halted = st.get("halted")
    paused = st.get("paused")
    if halted:
        state = f"{CORAL}● HALTED{RST}"
    elif paused:
        state = f"{GREY}● PAUSED{RST}"
    else:
        state = f"{GREEN}● ONLINE{RST}"

    wallet = float(st.get("sol_balance", 0.0) or 0.0)
    frac = wallet / PRESTIGE_GOAL
    daily = float(st.get("daily_pnl", 0.0) or 0.0)
    closed = int(st.get("closed_trades", 0) or 0)
    wins = int(st.get("bot_wins", 0) or 0)
    losses = int(st.get("bot_losses", 0) or 0)
    decisive = wins + losses
    wr = (wins / decisive * 100) if decisive else 0.0
    open_pos = st.get("open_positions") or {}

    closes, net = _recent_closes(bot, n_trades)

    # header (1-space right inset so state doesn't hug the border)
    head = bot_lbl + (" " * max(0, W - _visible_len(bot_lbl) - _visible_len(state) - 1)) + state
    out.append(top)
    out.append(_line(head, _visible_len(head)))
    out.append(mid)
    out.append(_line())

    # wallet + prestige progress
    out.append(_line(f"{GREY}WALLET{RST}        {BOLD}{WHITE}◎ {wallet:.4f}{RST}",
                     _visible_len(f"WALLET        ◎ {wallet:.4f}")))
    out.append(_line(f"{GREY}TO PRESTIGE{RST}   {DIM}◎ {PRESTIGE_GOAL:.1f}{RST}",
                     _visible_len(f"TO PRESTIGE   ◎ {PRESTIGE_GOAL:.1f}")))
    pct = f"{frac*100:.0f}%"
    bar = _bar(frac)
    out.append(_line(f"{bar}  {WHITE}{pct}{RST}", _visible_len("█" * 34) + 2 + len(pct)))
    out.append(_line())

    # headline stats
    dcol = _pnl_color(daily)
    dtag = "break-even" if abs(daily) < 1e-9 else ("up" if daily > 0 else "down")
    out.append(_line(f"{GREY}TODAY{RST}         {dcol}{daily:+.4f} ◎{RST}   {DIM}{dtag}{RST}",
                     _visible_len(f"TODAY         {daily:+.4f} ◎   {dtag}")))
    ncol = _pnl_color(net)
    out.append(_line(f"{GREY}REALIZED{RST}      {ncol}{net:+.4f} ◎{RST}   {DIM}net, all closes{RST}",
                     _visible_len(f"REALIZED      {net:+.4f} ◎   net, all closes")))
    wrtxt = f"{closed} trades · {wins}W / {losses}L · {wr:.0f}% win"
    out.append(_line(f"{GREY}RECORD{RST}        {WHITE}{wrtxt}{RST}",
                     _visible_len(f"RECORD        {wrtxt}")))
    if open_pos:
        op = f"{len(open_pos)} open"
    else:
        op = f"{DIM}none{RST}"
        op_vis = "none"
    out.append(_line(f"{GREY}OPEN{RST}          {WHITE}{op}{RST}" if open_pos
                     else f"{GREY}OPEN{RST}          {op}",
                     _visible_len(f"OPEN          {len(open_pos) if open_pos else 0} open") if open_pos
                     else _visible_len("OPEN          none")))
    out.append(_line())

    # recent trades
    out.append(mid)
    out.append(_line(f"{BOLD}{GREEN}RECENT TRADES{RST}", _visible_len("RECENT TRADES")))
    if not closes:
        out.append(_line(f"{GREY}no closed trades yet{RST}", _visible_len("no closed trades yet")))
    for d in closes:
        pnl = d.get("pnl_sol", 0.0) or 0.0
        arrow = "▲" if pnl > 1e-9 else ("▼" if pnl < -1e-9 else "■")
        col = _pnl_color(pnl)
        play = (d.get("play_v2") or d.get("play") or "—").lower()
        why = _exit_why(d.get("exit_reason"), d.get("hold_min"))
        row_plain = f"{arrow} {pnl:+.4f} ◎   {play:<9} {why}"
        row = f"{col}{arrow} {pnl:+.4f} ◎{RST}   {DIM}{play:<9}{RST} {GREY}{why}{RST}"
        out.append(_line(row, _visible_len(row_plain)))

    out.append(f"{DIM}╚{'═' * (W + 2)}╝{RST}")
    return "\n".join(out)


def _discover_bots():
    bdir = ROOT / "bots"
    found = []
    if bdir.exists():
        for p in sorted(bdir.glob("bot*/status.json")):
            try:
                found.append(int(p.parent.name.replace("bot", "")))
            except ValueError:
                pass
    return found or [1]


def main():
    ap = argparse.ArgumentParser(description="Dat Pip Boi terminal status viewer (read-only)")
    ap.add_argument("--bot", type=int, default=1, help="which bot (default 1)")
    ap.add_argument("--all", action="store_true", help="show every bot found")
    ap.add_argument("--trades", type=int, default=5, help="recent trades to show")
    args = ap.parse_args()

    bots = _discover_bots() if args.all else [args.bot]
    print()
    for b in bots:
        print(render_bot(b, args.trades))
        print()


if __name__ == "__main__":
    main()
