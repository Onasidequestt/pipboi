#!/usr/bin/env python3
"""
agent_runner.py — lightweight, paperclip-compatible reasoning-agent heartbeat.

Runs ONE agent for ONE heartbeat:
  1. assemble READ-ONLY context (run the fleet's own CLI tools + read state files)
  2. compose role-prompt (agents/roles/<agent>.md) + context into one prompt
  3. send to an LLM backend:  api  (anthropic SDK)  |  cli  (`claude -p`)  |  dry-run
  4. parse the strict-JSON recommendation, stamp it, write to shared_memory/agent_<name>.json
     and append to agents/history/<name>.jsonl  (+ token-cost ledger)

ADVISORY ONLY.  This runner never writes bot canaries, never arms genes, never touches
trades.db or any bots/botN/ file.  The agents PROPOSE; the operator / Risk Governor dispose.
This is the deliberate safety boundary of the prototype.

Paperclip migration:  agents/roles/<agent>.md IS the paperclip agent definition; the
CONTEXT_SOURCES below become the adapter's pre-prompt assembly; the JSON schema in the role
prompt is the task-result contract.  Nothing here is throwaway.

Usage:
  python3 agents/agent_runner.py strategy            # auto-selects backend
  python3 agents/agent_runner.py execution --dry-run # force inspect-only (writes the prompt)
  python3 agents/agent_runner.py strategy --backend api --model claude-sonnet-4-6
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ~/solana-trader
ROLES = Path(__file__).resolve().parent / "roles"
HIST = Path(__file__).resolve().parent / "history"
SHARED = ROOT / "shared_memory"
LOGS = ROOT / "logs"
DEFAULT_MODEL = "claude-sonnet-4-6"                     # routine agents: Sonnet, not Opus

# Per-agent context recipe. Commands run READ-ONLY from ROOT; stdout is captured + truncated.
# Files are tailed.  Keep the payload small — the tools already digest the heavy data
# (forward_obs.jsonl is 60MB+; we NEVER dump it, we read strategy_brain's digest of it).
CONTEXT_SOURCES = {
    "strategy": {
        "role": "strategy_researcher",
        "out": "agent_strategy.json",
        "commands": [
            ("strategy_brain --evaluate (candidate re-score, no state change)",
             ["python3", "strategy_brain.py", "--evaluate"]),
        ],
        "files": [
            ("shared_memory/strategy_brain.json (live_rule + promotion log)",
             SHARED / "strategy_brain.json", 8000),
        ],
        "prev": "agent_strategy.json",
    },
    "execution": {
        "role": "execution_engineer",
        "out": "agent_execution.json",
        "commands": [
            ("deep_pool_audit (LIVE realized vs SHADOW paper-EV + ghosts)",
             ["python3", "deep_pool_audit.py"]),
            ("edge_report --by-play (clean-era realized edge by play)",
             ["python3", "edge_report.py", "--by-play"]),
        ],
        "files": [],
        "prev": "agent_execution.json",
    },
}
CMD_TIMEOUT = 90          # seconds per context command
CMD_TRUNC = 6000          # max chars kept per command's stdout
PREV_TRUNC = 4000         # max chars of the previous brief fed back for delta-reasoning


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=CMD_TIMEOUT)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.returncode else "")
        return out.strip()[:CMD_TRUNC] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"(timed out after {CMD_TIMEOUT}s)"
    except Exception as e:                                              # noqa: BLE001
        return f"(failed: {e})"


def _tail(path: Path, n: int) -> str:
    try:
        data = path.read_text()
        return data[-n:] if len(data) > n else data
    except Exception:                                                   # noqa: BLE001
        return "(missing)"


def _forward_obs_digest() -> str:
    """Cheap streaming summary of forward_obs.jsonl — count + time span. Never load it whole."""
    fp = SHARED / "forward_obs.jsonl"
    if not fp.exists():
        return "(no forward_obs.jsonl)"
    n = 0
    first_ts = last_ts = None
    try:
        with fp.open() as fh:
            for line in fh:
                n += 1
                if n == 1:
                    try: first_ts = json.loads(line).get("ts")
                    except Exception: pass
        try: last_ts = json.loads(line).get("ts")  # noqa: F821  (last line)
        except Exception: pass
    except Exception as e:                                              # noqa: BLE001
        return f"(digest failed: {e})"
    span_h = (last_ts - first_ts) / 3600 if (first_ts and last_ts) else None
    return (f"forward_obs.jsonl: {n} observations, "
            f"span {span_h:.1f}h" if span_h else f"forward_obs.jsonl: {n} observations")


def assemble_context(agent: str) -> str:
    rec = CONTEXT_SOURCES[agent]
    blocks = [f"# HEARTBEAT CONTEXT — {agent} — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"]
    for label, cmd in rec["commands"]:
        blocks.append(f"## TOOL: {label}\n```\n{_run(cmd)}\n```\n")
    for label, path, n in rec["files"]:
        blocks.append(f"## STATE: {label}\n```\n{_tail(path, n)}\n```\n")
    if agent == "strategy":
        blocks.append(f"## STATE: forward-evidence digest\n```\n{_forward_obs_digest()}\n```\n")
    # previous brief, for delta reasoning
    prev = SHARED / rec["prev"]
    if prev.exists():
        blocks.append(f"## YOUR PREVIOUS BRIEF (reason about deltas vs this)\n"
                      f"```\n{_tail(prev, PREV_TRUNC)}\n```\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------- LLM backends
def _find_claude() -> str | None:
    """Locate the claude CLI even when ~/.local/bin isn't on a non-interactive PATH."""
    p = shutil.which("claude")
    if p:
        return p
    cand = Path.home() / ".local" / "bin" / "claude"
    return str(cand) if cand.exists() else None


def _select_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "api"
        except ImportError:
            pass
    if _find_claude():
        return "cli"
    return "dry-run"


def call_api(system: str, user: str, model: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=2000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    u = resp.usage
    usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
             "cache_read": getattr(u, "cache_read_input_tokens", 0),
             "cache_write": getattr(u, "cache_creation_input_tokens", 0)}
    return {"text": text, "usage": usage}


def call_cli(system: str, user: str, model: str) -> dict:
    # Mirrors paperclip's claude_local adapter: headless `claude -p`.
    # --system-prompt REPLACES Claude Code's default (clean reasoner, not a coding agent).
    # Run from a neutral cwd so it reasons ONLY over the context we provide (no repo crawl,
    # advisory boundary intact). --output-format json gives us real token usage + $ cost.
    claude = _find_claude()
    if not claude:
        raise RuntimeError("claude CLI not found (expected on PATH or ~/.local/bin/claude)")
    cmd = [claude, "-p", "--system-prompt", system,
           "--output-format", "json", "--model", model]
    p = subprocess.run(cmd, cwd=tempfile.gettempdir(), input=user,
                       capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise RuntimeError(f"claude CLI failed (rc={p.returncode}): "
                           f"{(p.stderr or p.stdout)[:500]}")
    payload = json.loads(p.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude returned error: {str(payload.get('result',''))[:300]}")
    u = payload.get("usage", {})
    usage = {"input_tokens": u.get("input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0),
             "cache_read": u.get("cache_read_input_tokens", 0),
             "cache_write": u.get("cache_creation_input_tokens", 0),
             "cost_usd": payload.get("total_cost_usd"),
             "session_id": payload.get("session_id")}
    return {"text": payload.get("result", ""), "usage": usage}


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        t = t[4:] if t.lower().startswith("json") else t
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(t[a:b + 1])


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Run one reasoning-agent heartbeat (advisory only).")
    ap.add_argument("agent", choices=list(CONTEXT_SOURCES))
    ap.add_argument("--backend", default="auto", choices=["auto", "api", "cli", "dry-run"])
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--dry-run", action="store_true", help="alias for --backend dry-run")
    args = ap.parse_args()

    HIST.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True)
    rec = CONTEXT_SOURCES[args.agent]
    system = (ROLES / f"{rec['role']}.md").read_text()
    user = assemble_context(args.agent)
    backend = "dry-run" if args.dry_run else _select_backend(args.backend)
    ts = int(time.time())
    print(f"[agent:{args.agent}] backend={backend} model={args.model} "
          f"context={len(user)} chars (~{len(user)//4} tok)")

    if backend == "dry-run":
        # Inspect-only: persist the EXACT payload so the operator can review (or paste into
        # a Claude session by hand) before spending a single token. Zero external dependency.
        pf = HIST / f"{args.agent}.last_prompt.txt"
        pf.write_text(f"<<<SYSTEM>>>\n{system}\n\n<<<USER>>>\n{user}\n")
        print(f"[agent:{args.agent}] DRY-RUN — wrote prompt to {pf}")
        # Report the TRUTH about backend availability — keyed on whether a live backend
        # actually exists, NOT on how dry-run was requested (--dry-run flag vs --backend dry-run).
        avail = _select_backend("auto")
        if avail != "dry-run":
            print(f"[agent:{args.agent}] inspect mode (dry-run); live backend AVAILABLE "
                  f"({avail}) — use --backend {avail} (or drop --dry-run) to run it.")
        else:
            print(f"[agent:{args.agent}] no backend wired. Add ANTHROPIC_API_KEY (pip install "
                  f"anthropic) or the `claude` CLI to go live.")
        return 0

    try:
        result = call_api(system, user, args.model) if backend == "api" \
            else call_cli(system, user, args.model)
        brief = _extract_json(result["text"])
    except Exception as e:                                              # noqa: BLE001
        err = {"agent": rec["role"], "ts": ts, "error": str(e),
               "raw": (locals().get("result", {}) or {}).get("text", "")[:1000]}
        (HIST / f"{args.agent}.jsonl").open("a").write(json.dumps(err) + "\n")
        print(f"[agent:{args.agent}] ERROR: {e}", file=sys.stderr)
        return 1

    # stamp + persist
    brief["ts"] = ts
    brief["_meta"] = {"backend": backend, "model": args.model,
                      "usage": result.get("usage", {}), "context_chars": len(user)}
    (SHARED / rec["out"]).write_text(json.dumps(brief, indent=2))
    (HIST / f"{args.agent}.jsonl").open("a").write(json.dumps(brief) + "\n")
    # token-cost ledger (answers "how much does this cost to run")
    if "input_tokens" in result.get("usage", {}):
        led = {"ts": ts, "agent": args.agent, "model": args.model, **result["usage"]}
        (HIST / "token_usage.jsonl").open("a").write(json.dumps(led) + "\n")
    print(f"[agent:{args.agent}] OK → {rec['out']}  summary: {brief.get('summary', '(none)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
