# deploy/ — LaunchAgents for reboot/login-durable uptime

The fleet and its loops are started by hand (`nohup ./run.sh …`, `keepalive.sh`, etc.) and **die on
reboot/logout**. These macOS LaunchAgents make them survive a reboot and auto-recover on crash. They
run in your GUI user session (so they fire missed jobs on wake — important on a sleeping laptop).

| plist | what it supervises | cadence |
|---|---|---|
| **com.vaultbot.fleet** | the WHOLE trading fleet — `run.sh` → sidecar + dashboard → bots (NEW) | RunAtLoad + KeepAlive |
| com.vaultbot.signallab | `signal_lab.py --log` evidence logger | KeepAlive |
| com.vaultbot.brain | `strategy_brain.py --evolve` hourly | StartInterval 3600 |
| com.vaultbot.watchdog | `fleet_watchdog.py` (rent reclaim / orphan sell / alerts) | StartInterval 21600 |
| com.vaultbot.optimizer | `run_optimizer.sh` (goldilocks param tuning) | KeepAlive |

## Install all (controlled window)

⚠ **Stop the manual fleet/loops first** so nothing double-runs, then bootstrap:

```bash
cd ~/solana-trader
pkill -f "run.sh"; pkill -f "main.py"; pkill -f "dashboard.py"; pkill -f "discovery_service.py"; pkill -f "caffeinate -dims"
pkill -f keepalive.sh; pkill -f "signal_lab.py --log"; pkill -f "strategy_brain.py --evolve"
sleep 2
cp deploy/com.vaultbot.*.plist ~/Library/LaunchAgents/
for L in fleet signallab brain watchdog optimizer; do
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vaultbot.$L.plist 2>/dev/null
  launchctl enable    gui/$(id -u)/com.vaultbot.$L
done
```

Just the fleet (most important one):

```bash
cp deploy/com.vaultbot.fleet.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vaultbot.fleet.plist
launchctl enable gui/$(id -u)/com.vaultbot.fleet
```

## The fleet agent's safety guard

`com.vaultbot.fleet` runs the **guarded** launcher `fleet_launchd.sh`, which **refuses to start a
second fleet when port 8080 is already bound**. launchd already runs ≤1 instance per Label, so the
only possible collision is launchd-vs-a-manual-`run.sh` — the guard catches exactly that, logging to
`logs/fleet_launchd.log` and exiting so KeepAlive + `ThrottleInterval 60` re-check every ~60s. It
converges (launches the moment the manual fleet stops) with **no tight respawn loop**. Combined with
the dashboard's own `_kill_orphans()` on boot and `run.sh`'s `kill 0` group teardown, you get: at
most one fleet, auto-recovered on death, auto-started on login/reboot. Verified: with a manual fleet
up, `lsof -ti tcp:8080` is bound → the guard correctly does not double-launch.

## Verify / control / remove

```bash
launchctl print gui/$(id -u)/com.vaultbot.fleet | grep -iE "state|pid"
curl -s localhost:8080 >/dev/null && echo "dashboard up"
launchctl kickstart -k gui/$(id -u)/com.vaultbot.fleet     # restart the fleet under the agent
launchctl bootout    gui/$(id -u)/com.vaultbot.fleet       # remove → back to manual run.sh
tail -f logs/fleet_launchd.log
```

## ⚠ What this does NOT fix: laptop SLEEP

A LaunchAgent restores the fleet on **reboot/login/crash**, but it does **not** keep a laptop awake.
`caffeinate -dims` (in run.sh) only blocks *idle* sleep while running — **closing the lid on battery
still sleeps**, and a sleeping fleet misses the rare regime windows that are the throughput edge.
For true 24/7, pick one:

- **Stay-awake (this machine):** `sudo pmset -a sleep 0 disablesleep 1` (or keep it plugged in with
  `pmset -c sleep 0`; revert `sudo pmset -a disablesleep 0`). Lid-closed-on-AC clamshell still sleeps
  unless an external display/keyboard is attached.
- **A $5/mo VPS (the real fix):** runs 24/7, no sleep, lower latency to RPC/Jupiter. Move the repo +
  `.env` + keypairs, install the agents (or systemd units), done. This is the durable answer for a
  system whose core constraint is *not missing* rare windows.
