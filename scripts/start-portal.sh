#!/usr/bin/env bash
# Start the full AI observability stack then open Mission Control portal.
# Used by ~/.config/autostart/observability-portal.desktop on login.

PROJ="/home/pk/Work/observability"
PORT=8080

wait_for() {
  local url="$1" label="$2" max="${3:-60}"
  echo "Waiting for ${label}..."
  for i in $(seq 1 "$max"); do
    curl -sf "$url" > /dev/null 2>&1 && echo "${label} ready." && return 0
    sleep 1
  done
  echo "WARNING: ${label} not ready after ${max}s — continuing anyway."
}

# 1. Start Docker stack (idempotent — safe to call if already running)
echo "Starting Docker services..."
docker compose -f "${PROJ}/docker-compose.yml" up -d

# 2. Wait for Prometheus (core dependency — all metrics flow through it)
wait_for "http://localhost:9090/-/healthy" "Prometheus" 90

# 3. Wait for Grafana
wait_for "http://localhost:3007/api/health" "Grafana" 60

# 4. Start ai_app.py if not already running on :8000
if ! lsof -ti:8000 > /dev/null 2>&1; then
  echo "Starting ai_app.py..."
  nohup python3 "${PROJ}/ai_app.py" > /tmp/ai_app.log 2>&1 &
  wait_for "http://localhost:8000/metrics" "ai_app" 30
else
  echo "ai_app already running on :8000"
fi

# 5. Start Claude session metrics exporter if not running on :8001
if ! lsof -ti:8001 > /dev/null 2>&1; then
  echo "Starting claude_metrics_exporter.py..."
  nohup python3 "${PROJ}/scripts/claude_metrics_exporter.py" \
    > /tmp/claude-exporter.log 2>&1 &
  sleep 2
else
  echo "Claude exporter already running on :8001"
fi

# 6. Start portal HTTP server if not running
if ! lsof -ti:${PORT} > /dev/null 2>&1; then
  echo "Starting portal HTTP server on :${PORT}..."
  nohup python3 -m http.server ${PORT} --directory "${PROJ}" \
    > /tmp/observability-portal.log 2>&1 &
  sleep 1
else
  echo "Portal server already running on :${PORT}"
fi

# 7. All services confirmed up — open browser
echo "Opening Mission Control portal..."
/usr/bin/brave-browser "http://localhost:${PORT}/portal/index.html"
