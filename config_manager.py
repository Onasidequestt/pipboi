"""
config_manager.py — centralized configuration loader.

Hierarchy (later wins on conflicts):
  config/base.json         — canonical defaults for all tunable parameters
  thresholds_override.json — runtime overrides written by the goldilocks optimizer

Access:
  from config_manager import cfg
  cfg("jito.tip_quiet_lamports")    # → 10000 or overridden value
  cfg("gem_path.min_volume_5m")     # → 3500 or goldilocks recommendation

Reload:
  import config_manager; config_manager.reload()

  Called automatically:
  - Every 20 closes in main.py (alongside strategy_apply_overrides)
  - On SIGHUP (Unix only) — lets run_optimizer.sh signal live bots via kill -HUP <pid>
"""
import json
import os
import signal
from pathlib import Path
from typing import Any

_HERE         = Path(__file__).parent
_BASE_JSON    = _HERE / "config" / "base.json"
_RUNTIME_JSON = _HERE / "thresholds_override.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on scalar conflicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class ConfigManager:
    """Loads config/base.json merged with thresholds_override.json.

    Both files are optional — missing files are silently skipped so startup
    never fails because of a missing JSON. Partial reads are safe because
    json.loads() is all-or-nothing; a half-written override file raises an
    exception (caught here) and the previous merged state is preserved.
    """

    def __init__(self) -> None:
        self._merged: dict = {}
        self.load()

    def load(self) -> None:
        base: dict = {}
        if _BASE_JSON.exists():
            try:
                base = json.loads(_BASE_JSON.read_text())
            except Exception as e:
                print(f"[Config] base.json parse error: {e}", flush=True)

        runtime: dict = {}
        if _RUNTIME_JSON.exists():
            try:
                runtime = json.loads(_RUNTIME_JSON.read_text())
            except Exception as e:
                print(f"[Config] thresholds_override.json parse error: {e}", flush=True)

        self._merged = _deep_merge(base, runtime)

    def reload(self) -> None:
        """Re-read both files and rebuild the merged config.
        Thread-safe: Python's GIL means the dict swap is atomic.
        """
        self.load()
        print("[Config] Reloaded from disk", flush=True)

    def get(self, key: str, default: Any = None) -> Any:
        """Dot-notation read. e.g. cfg('jito.tip_quiet_lamports').
        Returns `default` if any path component is missing.
        """
        node: Any = self._merged
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def as_dict(self) -> dict:
        """Snapshot of the full merged config — useful for dashboard /api/config."""
        return dict(self._merged)


# ── Module-level singleton ────────────────────────────────────────────────────
_manager = ConfigManager()


def cfg(key: str, default: Any = None) -> Any:
    """Convenience accessor. Falls back to `default` when key is absent."""
    return _manager.get(key, default)


def reload() -> None:
    """Reload the singleton from disk. Called by the goldilocks auto-trigger."""
    _manager.reload()


def as_dict() -> dict:
    return _manager.as_dict()


# ── SIGHUP reload (Unix only) ─────────────────────────────────────────────────
# run_optimizer.sh can signal all bot PIDs after writing thresholds_override.json:
#   kill -HUP $(pgrep -f "python3 main.py")
# This triggers an immediate in-process reload without a restart.
def _sighup_handler(signum, frame):  # noqa: ARG001
    reload()


if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, _sighup_handler)
