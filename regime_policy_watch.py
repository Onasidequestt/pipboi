#!/usr/bin/env python3
"""
regime_policy_watch.py — REGIMEPOLICY task D: the pre-registered, hash-pinned, fail-closed
verdict on the staged play×depth policy surface (runner_watch/lane_watch idiom).

WHAT WOULD PROVE THE NEW REGIME MODEL LIVE (pre-registered BEFORE any canary flips):
  PROVE-CANDIDATE  n≥30 governed sized closes ∧ ≥10 distinct mints ∧ no mint >40% of the
                   cohort ∧ ghost-rate ≤10% ∧ per-mint-deduped mean ret% ≥ baseline+3pp ∧
                   0 admission leaks → suggest keeping the policy + widening the canary.
  KILL             n≥30 ∧ deduped mean < baseline−3pp → suggest `rm bots/bot*/regime_policy.json`.
  PENDING          otherwise (prints the binding constraint).

LEAK INVARIANT (the dpgap lesson, made a tripwire): any governed close opened after enable
whose entry_liq maps to an admit=false depth cell is an ADMISSION LEAK — some entry path
bypassed the policy hook. Any leak blocks PROVE and exits nonzero.

READ-ONLY on the fleet: writes only regime_policy_watch.json (its own criterion/state) +
regime_policy_watch_log.jsonl. It NEVER acts — verdicts print suggested actions only.
Goalposts: the criterion fields AND the policy-table bytes are sha-pinned at scaffold; any
drift prints a loud GOALPOST-MOVED warning. Baseline = the pre-enable proceeds-era governed
cohort (era-mismatch caveat documented in FINDINGS §D).

Exit codes: 0 PENDING/PROVE-CANDIDATE · 2 KILL · 3 LEAK.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import regime_policy as rp
import prestige_tracker as _pt   # _is_ghost — the single ghost definition

CRIT_PATH = BASE / "regime_policy_watch.json"
LOG_PATH = BASE / "regime_policy_watch_log.jsonl"

_CRITERION_FIELDS = ("n_min", "mints_min", "top_share_max", "ghost_max",
                     "improve_pp", "deadline", "table_sha", "baseline_dedup_mean")
GOVERNED_PLAYS = frozenset({"gem", "momentum", "relay", "quick", "bank",
                            "mean_reversion", "highconv"})


def _crit_hash(c):
    blob = json.dumps({k: c.get(k) for k in _CRITERION_FIELDS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _governed(r):
    """A close row this policy governs: price-action play OR the momentum_override lane.
    Frozen plays (deep_pool/brain_rule/normal_slice/dust_shadow) are NEVER counted."""
    if r.get("dust_shadow") or r.get("normal_slice"):
        return False
    play = r.get("play") or r.get("tier")
    if play in ("deep_pool", "brain_rule"):
        return False
    return bool(r.get("momentum_override")) or play in GOVERNED_PLAYS


def _closes(bot, min_rowid=0):
    out = []
    dbp = BASE / "bots" / f"bot{bot}" / "trades.db"
    if not dbp.exists():
        return out
    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    for rid, d in con.execute("SELECT rowid,data FROM trades WHERE event='close' ORDER BY rowid"):
        if rid <= min_rowid:
            continue
        try:
            r = json.loads(d)
        except Exception:
            continue
        r["_rowid"] = rid
        out.append(r)
    con.close()
    return out


def _proceeds_governed(rows):
    return [r for r in rows
            if r.get("realized_sol_out") is not None
            and (r.get("size_sol") or 0) > 0 and _governed(r)]


def _dedup_mean(samples):
    """samples: (mint, ret%) → per-mint-collapsed mean + concentration stats."""
    acc = defaultdict(list)
    for m, v in samples:
        acc[m].append(v)
    if not acc:
        return None, 0, 0.0
    per_mint = [sum(v) / len(v) for v in acc.values()]
    top_share = max(len(v) for v in acc.values()) / len(samples)
    return sum(per_mint) / len(per_mint), len(per_mint), top_share


def scaffold():
    """Pin the criterion + the pre-enable baseline. Refuses to overwrite an existing pin."""
    if CRIT_PATH.exists():
        return json.loads(CRIT_PATH.read_text())
    samples = []
    for b in (1, 2, 3):
        for r in _proceeds_governed(_closes(b)):
            if not _pt._is_ghost(r):
                samples.append((r.get("mint"), r["pnl_sol"] / r["size_sol"] * 100.0))
    base_mean, base_mints, _ = _dedup_mean(samples)
    crit = {
        "_note": "Pre-registered BEFORE any regime_policy canary was enabled. The four bar "
                 "fields + deadline + table_sha + baseline are FINAL — moving them re-trips "
                 "the GOALPOST-MOVED warning. enable_rowids/_note are excluded from the hash.",
        "created": datetime.now(timezone.utc).isoformat(),
        "n_min": 30, "mints_min": 10, "top_share_max": 0.40, "ghost_max": 0.10,
        "improve_pp": 3.0,
        "deadline": (datetime.now(timezone.utc) + timedelta(days=45)).date().isoformat(),
        "table_sha": rp.table_hash(),
        "baseline_dedup_mean": None if base_mean is None else round(base_mean, 3),
        "baseline_n": len(samples), "baseline_mints": base_mints,
        "enable_rowids": {},
    }
    crit["criterion_hash"] = _crit_hash(crit)
    CRIT_PATH.write_text(json.dumps(crit, indent=1))
    print(f"[scaffold] criterion pinned: hash={crit['criterion_hash']} table_sha={crit['table_sha']} "
          f"baseline={crit['baseline_dedup_mean']}pp (n={crit['baseline_n']}/{base_mints} mints)")
    return crit


def main():
    crit = scaffold()

    # goalpost guards
    warn = []
    if _crit_hash(crit) != crit.get("criterion_hash"):
        warn.append(f"⚠ GOALPOST-MOVED: criterion fields changed (hash {_crit_hash(crit)} ≠ pinned {crit.get('criterion_hash')})")
    live_sha = rp.table_hash()
    if live_sha != crit.get("table_sha"):
        warn.append(f"⚠ GOALPOST-MOVED: regime_policy_table.json changed (sha {live_sha} ≠ pinned {crit.get('table_sha')}) — the cohort no longer matches the pinned policy")
    for w in warn:
        print(w)

    # record enable rowids the first time a canary is seen enabled (write-once per bot)
    changed = False
    for b in (1, 2, 3):
        key = str(b)
        if key in crit["enable_rowids"]:
            continue
        if rp._cfg(b):
            rows = _closes(b)
            crit["enable_rowids"][key] = rows[-1]["_rowid"] if rows else 0
            changed = True
            print(f"[arm] bot{b} canary detected enabled → cohort starts at rowid {crit['enable_rowids'][key]}")
    if changed:
        CRIT_PATH.write_text(json.dumps(crit, indent=1))

    if not crit["enable_rowids"]:
        verdict = {"verdict": "PENDING", "why": "no bot has the regime_policy canary enabled — cohort n=0/{}".format(crit["n_min"])}
        print(f"⏳ PENDING — {verdict['why']}")
        _log(crit, verdict)
        return 0

    # cohort: governed sized proceeds closes after each bot's enable rowid
    # TAXONOMY v2 leak adjudication: a governed close is a LEAK when its v2 cell could
    # never have admitted it — DEEP (frozen) · a play whose cells skip in the row's band
    # (regime_v2 tag when stamped, name-shim fallback; no band resolvable → leak only if
    # EVERY band skips) · entry_liq past the play's depth belt.
    cohort, ghosts, leaks = [], 0, []
    table = json.loads((BASE / "regime_policy_table.json").read_text())

    def _row_leak(r):
        p2 = rp.play_v2(r.get("play") or r.get("tier"),
                        momentum_override=bool(r.get("momentum_override")),
                        normal_slice=bool(r.get("normal_slice")),
                        dust_shadow=bool(r.get("dust_shadow")))
        if p2 == "DUST":
            return None
        if p2 == "DEEP":
            return f"{p2} (frozen)"
        belt = (table.get("depth_belts", {}).get(p2) or {}).get("max_liq")
        liq = r.get("entry_liq")
        if belt is not None and liq is not None and float(liq) >= float(belt):
            return f"{p2} depth belt ≥{belt}"
        cells = table.get("grid", {}).get(p2, {})
        r2 = r.get("regime_v2") or rp.regime_v2_from_legacy(r.get("regime"))
        if r2 and r2 in cells:
            if cells[r2].get("admit") in ("skip", "frozen"):
                return f"{p2}×{r2} admit={cells[r2].get('admit')}"
            return None
        if cells and all(c.get("admit") in ("skip", "frozen") for c in cells.values()):
            return f"{p2} (no admitting band)"
        return None

    for b_str, rid0 in crit["enable_rowids"].items():
        for r in _proceeds_governed(_closes(int(b_str), min_rowid=rid0)):
            if _pt._is_ghost(r):
                ghosts += 1
                continue
            why = _row_leak(r)
            if why:
                leaks.append({"bot": b_str, "rowid": r["_rowid"], "mint": r.get("mint"),
                              "cell": why})
            cohort.append((r.get("mint"), r["pnl_sol"] / r["size_sol"] * 100.0))

    n = len(cohort) + ghosts
    ghost_rate = (ghosts / n) if n else 0.0
    mean, mints, top_share = _dedup_mean(cohort)
    base = crit.get("baseline_dedup_mean")
    print(f"cohort: n={n} (clean {len(cohort)}) mints={mints} dedup_mean="
          f"{'·' if mean is None else f'{mean:+.2f}pp'} top_share={top_share:.0%} "
          f"ghost={ghost_rate:.0%} | baseline={base}pp | leaks={len(leaks)}")

    if leaks:
        for l in leaks[:10]:
            print(f"  🚨 ADMISSION LEAK: bot{l['bot']} rowid {l['rowid']} {l['mint'][:8]}… → {l['cell']}")
        verdict = {"verdict": "LEAK", "why": f"{len(leaks)} governed close(s) in admit=false cells — an entry path bypassed the policy hook (dpgap-class)", "leaks": leaks}
        print(f"🚨 LEAK — {verdict['why']}")
        _log(crit, verdict, n=n, mean=mean, mints=mints)
        return 3

    if n >= crit["n_min"] and mean is not None and base is not None:
        if (mean >= base + crit["improve_pp"] and mints >= crit["mints_min"]
                and top_share <= crit["top_share_max"] and ghost_rate <= crit["ghost_max"]):
            verdict = {"verdict": "PROVE-CANDIDATE",
                       "why": f"dedup {mean:+.2f} ≥ baseline {base:+.2f}+{crit['improve_pp']} across {mints} mints",
                       "suggest": "keep the policy; consider widening the canary (operator call)"}
            print(f"✅ PROVE-CANDIDATE — {verdict['why']}")
            _log(crit, verdict, n=n, mean=mean, mints=mints)
            return 0
        if mean < base - crit["improve_pp"]:
            verdict = {"verdict": "KILL",
                       "why": f"dedup {mean:+.2f} < baseline {base:+.2f}−{crit['improve_pp']} at n={n}",
                       "suggest": "rm bots/bot*/regime_policy.json (hot, ≤30s — constants resume)"}
            print(f"🔴 KILL — {verdict['why']}\n   suggested action (NOT taken): {verdict['suggest']}")
            _log(crit, verdict, n=n, mean=mean, mints=mints)
            return 2

    binding = (f"n={n}/{crit['n_min']}" if n < crit["n_min"] else
               f"mints={mints}/{crit['mints_min']}" if (mints or 0) < crit["mints_min"] else
               f"top_share={top_share:.0%}>{crit['top_share_max']:.0%}" if top_share > crit["top_share_max"] else
               "mean within ±improve_pp of baseline — not yet decisive")
    if crit["deadline"] < datetime.now(timezone.utc).date().isoformat():
        print(f"⏰ DEADLINE PASSED ({crit['deadline']}) without PROVE — honest default: rm the canaries.")
    verdict = {"verdict": "PENDING", "why": f"binding constraint: {binding}"}
    print(f"⏳ PENDING — {verdict['why']}")
    _log(crit, verdict, n=n, mean=mean, mints=mints)
    return 0


def _log(crit, verdict, **extra):
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "criterion_hash": crit.get("criterion_hash"),
                                 "table_sha": rp.table_hash(), **verdict, **extra}) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
