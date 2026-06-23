#!/usr/bin/env bash
# Keeps the dashboard tunnel up on a FIXED ngrok domain. Auto-restarts if ngrok drops.
# Dies on reboot (like the other loops). Stop: pkill -f ngrok_keepalive ; pkill -f "ngrok http"
DOMAIN="gallery-snooze-scalded.ngrok-free.dev"
LOG="$HOME/solana-trader/.ngrok_tunnel.log"
while true; do
  echo "[keepalive] starting ngrok -> https://$DOMAIN $(date)" >> "$LOG"
  ngrok http 8080 --url="https://$DOMAIN" --log=stdout >> "$LOG" 2>&1
  echo "[keepalive] ngrok exited, restarting in 5s $(date)" >> "$LOG"
  sleep 5
done
