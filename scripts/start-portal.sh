#!/usr/bin/env bash
# Start the AI observability portal and open in Brave.
# Placed in ~/.config/autostart/ via observability-portal.desktop

PROJ="/home/pk/Work/observability"
PORT=8080

# Wait up to 40s for Prometheus to be reachable (Docker may still be starting)
for i in $(seq 1 40); do
  curl -sf http://localhost:9090/-/healthy > /dev/null 2>&1 && break
  sleep 1
done

# Start the portal HTTP server only if the port is free
if ! lsof -ti:${PORT} > /dev/null 2>&1; then
  nohup python3 -m http.server ${PORT} --directory "${PROJ}" \
    > /tmp/observability-portal.log 2>&1 &
  sleep 1
fi

# Start Claude session metrics exporter if not running
if ! lsof -ti:8001 > /dev/null 2>&1; then
  nohup python3 "${PROJ}/scripts/claude_metrics_exporter.py" \
    > /tmp/claude-exporter.log 2>&1 &
fi

# Open Brave to the portal
/usr/bin/brave-browser "http://localhost:${PORT}/portal/index.html"
