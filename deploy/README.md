# deploy/ — optional macOS LaunchAgents for reboot/login-durable uptime

`./run.sh` (or `./pipboi run`) runs PIPBOI in a terminal and **stops when you close it**. These
optional macOS LaunchAgents make it survive reboot/logout and auto-recover on crash, running in your
GUI user session (so missed jobs fire on wake — handy on a sleeping laptop). **None of this is
required** to run the bot.

| agent | what it supervises | cadence |
|---|---|---|
| **com.pipboi.fleet** | the whole bot — `run.sh` → sidecar + dashboard + bot | RunAtLoad + KeepAlive |
| com.pipboi.signallab | `signal_lab.py --log` evidence logger | KeepAlive |
| com.pipboi.brain | `strategy_brain.py --evolve` hourly | StartInterval 3600 |
| com.pipboi.watchdog | `fleet_watchdog.py` (orphan sell / alerts) | StartInterval 21600 |
| com.pipboi.optimizer | `run_optimizer.sh` (param tuning) | KeepAlive |

## Install

The installer fills this repo's real path into the plist templates and loads them — no `sudo`, no
system-wide changes (everything lives under `~/Library/LaunchAgents` and runs as you):

```bash
./deploy/install.sh          # install + start all agents
./deploy/install.sh fleet    # just the main fleet agent (the important one)
./deploy/uninstall.sh        # remove them all (code, .env, and trade data untouched)
```

The committed `.plist` files are **templates** (they contain `__PIPBOI_DIR__`, not a real path) so
nothing machine-specific is baked into the repo. `install.sh` substitutes your actual repo path at
install time.

## The fleet agent's safety guard

`com.pipboi.fleet` runs the guarded launcher `fleet_launchd.sh`, which **refuses to start a second
fleet when port 8080 is already bound**. launchd runs ≤1 instance per Label, so the only possible
collision is launchd-vs-a-manual-`run.sh` — the guard catches exactly that, logs to
`src/logs/fleet_launchd.log`, and exits so KeepAlive re-checks ~every 60s. It converges (starts the
moment the manual fleet stops) with no tight respawn loop.

## Verify / control

```bash
launchctl list | grep com.pipboi
launchctl print gui/$(id -u)/com.pipboi.fleet | grep -iE "state|pid"
curl -s localhost:8080 >/dev/null && echo "dashboard up"
launchctl kickstart -k gui/$(id -u)/com.pipboi.fleet     # restart under the agent
tail -f src/logs/fleet_launchd.log
```

## ⚠ What this does NOT fix: laptop SLEEP

A LaunchAgent restores the bot on reboot/login/crash, but does not keep a laptop awake.
`caffeinate -dims` (in `run.sh`) only blocks *idle* sleep while running — **closing the lid on
battery still sleeps**. For true 24/7, either keep the machine plugged in and awake
(`sudo pmset -a sleep 0 disablesleep 1`; revert with `disablesleep 0`), or run on a small always-on
Linux box/VPS (move the repo + `.env` + keypair, then use the equivalent systemd units).
