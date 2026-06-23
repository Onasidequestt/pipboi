#!/usr/bin/env python3
"""
lanes.py — S121 V2 LANE REGISTRY (read-only data layer).

A "lane" is a NAMED entry strategy with its own admission belts, veto stack, exit params, size
multiplier, and a pre-registered watcher. This module is the single registry the rest of V2 reads:
  • lane_watch.py     judges each lane's live cohort against its pre-registered verdict.
  • dashboard /api/lanes surfaces lane rung + verdict.
  • (future) observer admission + main sizing consult it directly.

⚠ ONE-RELEASE ALIAS: in this release the live admission/exit/size plumbing is STILL the existing
per-lane implementation (the runner lane = the S107/S118 `momentum_override` threading). lanes.py is
the descriptive/verdict layer over it — `alias_field` ties a lane to the close-row flag its
implementation already stamps (`momentum_override`, `normal_slice`), and `canary` ties it to the live
toggle file. Wiring lanes.py INTO observer admission (replacing that bespoke threading with a generic
`lane=` field) is the next-release step; doing it now would be risky surgery on live admission code.

READ-ONLY: loads JSON specs from lanes/*.json. Touches no DB, no trading, no ev_sizing.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LANES_DIR = ROOT / "lanes"


def _load_one(p: Path) -> dict | None:
    try:
        spec = json.loads(p.read_text())
        spec.setdefault("name", p.stem)
        return spec
    except Exception:
        return None


def registry() -> dict:
    """{name: spec} for every readable lanes/*.json (fail-soft — a bad file is skipped)."""
    out: dict = {}
    if not LANES_DIR.exists():
        return out
    for p in sorted(LANES_DIR.glob("*.json")):
        spec = _load_one(p)
        if spec and spec.get("name"):
            out[spec["name"]] = spec
    return out


def get(name: str) -> dict | None:
    return registry().get(name)


def lane_names() -> list:
    return sorted(registry().keys())


def cohort_match(close_row: dict, spec: dict) -> bool:
    """True if a close audit row belongs to this lane. Matches the generic `lane` field (next-release)
    OR the lane's `alias_field` flag its current implementation stamps (this-release)."""
    name = spec.get("name")
    if close_row.get("lane") == name:
        return True
    alias = spec.get("alias_field")
    return bool(alias and close_row.get(alias))


def canary_enabled(spec: dict, bot: int) -> bool:
    """Is the lane's LIVE toggle on for this bot? Reads bots/botN/<canary> {"enabled":true}.
    A lane with no `canary` (a pure spec/shadow lane) is never 'live'. Fail-soft → False."""
    cfile = spec.get("canary")
    if not cfile:
        return False
    try:
        fp = ROOT / f"bots/bot{bot}" / cfile
        if not fp.exists():
            return False
        return bool((json.loads(fp.read_text()) or {}).get("enabled"))
    except Exception:
        return False


def rung(spec: dict, bot: int = 1) -> str:
    """Promotion rung for the dashboard: off / shadow / dust / canary / fleet.
    Derived from the spec's `rung` hint + whether the live canary is on (so it can't claim 'fleet'
    while its toggle is off)."""
    declared = (spec.get("rung") or "off").lower()
    live = canary_enabled(spec, bot)
    if declared in ("dust", "shadow") and not live:
        return declared
    if declared == "fleet":
        return "fleet" if all(canary_enabled(spec, b) for b in (1, 2, 3)) else ("canary" if live else "off")
    if declared == "canary":
        return "canary" if live else "off"
    return declared if not live else "canary"


if __name__ == "__main__":
    reg = registry()
    print(f"  {len(reg)} lane(s) registered in {LANES_DIR}:")
    for name, spec in reg.items():
        print(f"   • {name:10s} rung={rung(spec):8s} alias={spec.get('alias_field')} "
              f"canary={spec.get('canary')} size×{spec.get('size_mult')}")
