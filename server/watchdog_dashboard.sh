#!/bin/bash
# Hermes Voice dashboard watchdog — run from launchd every 60 s on the AGENT
# machine. Heals the two things that silently kill remote voice clients:
#   1. the dashboard backend wedging (port open, HTTP starved) — 3-strike
#      kickstart so long legitimate pauses are never killed;
#   2. the tailscale proxy being dead or never loaded — stateless forwarder,
#      restarting is free, healed immediately.
# Configure the four vars below (or export them in the plist).
LOG="$HOME/.hermes/logs/watchdog-dashboard.log"
STATE="/tmp/watchdog-hermes-dashboard.fails"
BACKEND_LABEL="${BACKEND_LABEL:-ai.hermes.gateway}"          # launchd label serving the dashboard
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:9128/}"         # dashboard on loopback
PROXY_LABEL="${PROXY_LABEL:-ai.hermes.dashboard-tailscale-proxy}"
PROXY_URL="${PROXY_URL:-http://100.x.y.z:9119/}"             # dashboard as clients see it

code=$(curl -m 5 -s -o /dev/null -w '%{http_code}' "$BACKEND_URL" 2>/dev/null)
if [ "$code" = "200" ]; then
  rm -f "$STATE"
  # Backend healthy → verify the client-facing proxy end-to-end. Only checked
  # when the backend is up, so a proxy-path failure is never misblamed.
  pcode=$(curl -m 5 -s -o /dev/null -w '%{http_code}' "$PROXY_URL" 2>/dev/null)
  if [ "$pcode" != "200" ]; then
    if launchctl print "gui/$(id -u)/$PROXY_LABEL" >/dev/null 2>&1; then
      echo "$(date '+%F %T') proxy dead (http=$pcode) — kickstart -k $PROXY_LABEL" >> "$LOG"
      launchctl kickstart -k "gui/$(id -u)/$PROXY_LABEL" >> "$LOG" 2>&1
    else
      echo "$(date '+%F %T') proxy NOT LOADED (http=$pcode) — load -w $PROXY_LABEL" >> "$LOG"
      launchctl load -w "$HOME/Library/LaunchAgents/$PROXY_LABEL.plist" >> "$LOG" 2>&1
    fi
  fi
  exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "$(date '+%F %T') backend health fail #$fails (http=$code)" >> "$LOG"

if [ "$fails" -lt 3 ]; then
  exit 0
fi

echo "$(date '+%F %T') kickstart -k $BACKEND_LABEL" >> "$LOG"
launchctl kickstart -k "gui/$(id -u)/$BACKEND_LABEL" >> "$LOG" 2>&1
rm -f "$STATE"
