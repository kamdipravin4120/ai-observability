# AI Observability Stack — Local Mission Control

> **GitHub:** https://github.com/kamdipravin4120/ai-observability

A **Level 4** closed-loop AI observability system. Prometheus metrics, Grafana dashboards, Loki logs, Tempo traces, and an automated policy engine that reroutes traffic between models when latency, cost, or error thresholds are crossed — no human in the loop.

---

## Architecture

```
ai_app.py ──► Prometheus ──► Policy Engine ──► Router ──► Model call
    │                              ▲                │
    │         (background          │    feedback    │
    ├──► Loki (structured logs) ───┘                │
    └──► Tempo (OTEL traces) ◄──────────────────────┘
```

Grafana sits on top of Prometheus + Loki + Tempo and drives dashboards, alerts, and the mission control portal.

---

## Stack Components

| Service        | Image                          | Port      | Purpose                        |
|----------------|--------------------------------|-----------|--------------------------------|
| Prometheus     | prom/prometheus                | 9090      | Metrics storage & querying     |
| Grafana        | grafana/grafana                | 3007      | Dashboards, alerts, explore    |
| Loki           | grafana/loki                   | 3100      | Log aggregation                |
| Promtail       | grafana/promtail               | 9080      | Log shipping to Loki           |
| Tempo          | grafana/tempo                  | 3200      | Distributed trace storage      |
| OTel Collector | otel/opentelemetry-collector   | 4317/4318 | OTLP trace ingestion           |
| Node Exporter  | prom/node-exporter             | 9100      | Host system metrics            |
| ai_app.py      | (bare-metal Python)            | 8000      | Metrics endpoint + simulator   |
| Portal         | (python3 http.server)          | 8080      | Mission control web portal     |

---

## Project Structure

```
observability/
├── ai_app.py              # Main app: simulator, policy engine, OTEL tracing, JSON logging
├── docker-compose.yml     # All Docker services
├── readme.md
│
├── config/
│   ├── prometheus.yml      # Scrape config (target: 172.17.0.1:8000)
│   ├── promtail-config.yml # Log scrape jobs: system + ai_app + ai_decisions
│   ├── otel-config.yml     # OTLP receiver → Tempo exporter
│   ├── tempo.yml           # Trace storage config
│   └── policies.yml        # Declarative routing policies + model map
│
├── dashboards/
│   ├── grafana-dashboard.json       # v4 — 19-panel AI observability dashboard
│   └── claude-sessions-dashboard.json  # Claude Code session analytics
│
├── docs/
│   ├── PLAN.md             # Level 4 implementation plan
│   └── network.md          # Full port/service inventory
│
├── logs/                   # Runtime logs (gitignore this dir)
│   ├── ai_app.log          # Per-request JSON (→ Loki stream: requests)
│   └── ai_decisions.log    # Policy decisions JSON (→ Loki stream: decisions)
│
├── portal/
│   └── index.html          # Mission control portal — live metrics, auto-refresh
│
└── scripts/
    ├── setup_alerts.py     # Provisions 3 Grafana alert rules via API
    └── start-portal.sh     # Startup script: HTTP server + Brave autostart
```

---

## Quick Start

### 1. Start Docker services

```bash
docker compose up -d
```

### 2. Start the AI simulator

```bash
cd /home/pk/Work/observability
nohup python3 ai_app.py > /tmp/ai_app.log 2>&1 &
```

### 3. Verify metrics are flowing

```bash
curl -s http://localhost:8000/metrics | grep -E "ai_requests|ai_active_model|ai_model_switches"
```

### 4. Import the Grafana dashboard

1. Open Grafana: http://localhost:3007
2. Go to **Dashboards → Import**
3. Upload `dashboards/grafana-dashboard.json`
4. Map **DS_PROMETHEUS** → your Prometheus datasource
5. Map **DS_LOKI** → your Loki datasource

### 5. Provision alert rules

```bash
python3 scripts/setup_alerts.py \
  --url http://localhost:3007 \
  --user <user> \
  --password <pass>
```

### 6. Open the mission control portal

```bash
bash scripts/start-portal.sh
```

Or visit http://localhost:8080/portal/index.html after the portal server is running.

---

## Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ai_requests_total` | Counter | `provider`, `model` | Total AI requests |
| `ai_errors_total` | Counter | `provider`, `model` | Failed requests |
| `ai_latency_seconds` | Histogram | `provider`, `model` | Request latency |
| `ai_tokens_in_total` | Counter | `provider`, `model` | Input tokens consumed |
| `ai_tokens_out_total` | Counter | `provider`, `model` | Output tokens generated |
| `ai_cost_usd_total` | Counter | `provider`, `model` | Estimated cost in USD |
| `ai_active_model` | Gauge | `category`, `model` | 1 for currently active model |
| `ai_model_switches_total` | Counter | `from_model`, `to_model`, `reason` | Policy-driven switch events |

---

## Routing Policy Engine

Defined in `config/policies.yml`. Runs as a background thread in `ai_app.py`, polling Prometheus every 10 seconds.

```yaml
cooldown_seconds: 60      # Min seconds between switches (prevents flapping)

models:
  default:  claude-sonnet-4-6   # Normal traffic
  fast:     claude-haiku-4-5    # High-latency fallback
  cheap:    codex-mini-latest   # High-cost fallback
  fallback: gpt-4o              # High-error fallback

policies:
  - name: high_errors    condition: "error_rate > 0.03"   action: fallback  priority: 1
  - name: high_latency   condition: "p95_latency > 2.0"   action: fast      priority: 2
  - name: high_cost      condition: "cost_per_min > 0.1"  action: cheap     priority: 3
```

**How it works:**
1. Every 10s, queries Prometheus for `error_rate`, `p95_latency`, `cost_per_min`
2. Evaluates policies in priority order (1 = highest)
3. First matching condition wins → switches active model category
4. Cooldown prevents rapid back-and-forth switching
5. Every decision (including no-change) logged to `logs/ai_decisions.log` → Loki

**Safe condition parser** — whitelist-only regex `(metric) (op) (number)`. Conditions are parsed into `(var, op, threshold)` tuples and dispatched manually — never executed as dynamic code. Supports `>`, `>=`, `<`, `<=`, `==`, `!=`.

---

## Log Streams (Loki)

| File | Loki labels | Content |
|------|-------------|---------|
| `logs/ai_app.log` | `job=ai_app, stream=requests` | Per-request: ts, provider, model, category, latency, tokens, cost, error |
| `logs/ai_decisions.log` | `job=ai_app, stream=decisions` | Per-evaluation: ts, msg (no_change/cooldown/switched), metrics snapshot, reason |

Query in Grafana Explore → Loki:
```logql
{job="ai_app", stream="decisions"} | json | msg="switched"
{job="ai_app", stream="requests", level="ERROR"}
```

---

## OTEL Tracing

Traces sent via gRPC to `localhost:4317` (OTel Collector → Tempo).

> **Note:** If running ai_app.py bare-metal and traces don't appear in Tempo, change the OTLP endpoint in `ai_app.py` from `localhost:4317` to `172.17.0.1:4317` (Docker gateway IP).

View in Grafana: **Explore → Tempo → Service Name: ai_app**

Each span includes: `category`, `provider`, `model`, `tokens.input`, `tokens.output` attributes.

---

## Grafana Dashboard (v4 — 19 panels)

| Panel | Type | Content |
|-------|------|---------|
| 1–4 | Stat / Gauge | Requests/sec, Error rate gauge, Cost/min, P95 latency gauge |
| 5 | Timeseries | Requests/sec by model |
| 6 | Timeseries | Error rate by model — amber band >1%, red band >3% |
| 7 | Timeseries | P95 + P50 latency — amber >1.5s, red >2s, dashed P50 lines |
| 8 | Timeseries | Avg latency with threshold bands |
| 9–10 | Timeseries | Token throughput (in/out) |
| 11–12 | Timeseries + Bar gauge | Cost by model + LCD cost bar |
| 13–14 | Timeseries | Provider-level cost + tokens |
| 15 | Stat | **Active Model** (`topk(1, ai_active_model == 1)`) |
| 16 | Timeseries | **Model Switches/hr** by reason |
| 17 | Timeseries | **Cost per Request** by model |
| 18 | Logs | **Decision Timeline** — Loki `stream=decisions` |
| 19 | Logs | **Error Logs** — Loki `stream=requests, level=ERROR` |

---

## Alert Rules (setup_alerts.py)

Provisioned to Grafana unified alerting under folder **AI Alerts**, group **AI Observability**:

| Rule | Condition | For | Severity |
|------|-----------|-----|----------|
| High Error Rate | error rate > 2% | 2 min | critical |
| High P95 Latency | P95 > 2 s | 2 min | warning |
| Cost Spike | cost > $0.10/min | 2 min | warning |

---

## Mission Control Portal

Custom dashboard portal at **http://localhost:8080/portal/index.html**.

"Quantum Command" aesthetic — full viewport, no page scroll, 3-column layout.

**Layout:**
- **Top bar** — 6 service health pills, live/dead indicator, realtime clock, last-updated timestamp
- **Left sidebar** — 7-service health grid (Prometheus, Grafana, Loki, Tempo, AI App, Claude Exporter, OTel), system uptime, build version, control loop mode
- **Center** — animated SVG orbital rings + 4 satellite dots around the active model display; 8 live metric cards (RPS, error rate, P95 latency, cost/min, tokens/s, input tok/s, output tok/s, model switches) each with a live SVG sparkline (60-point ring buffer, 5s tick)
- **Right sidebar** — routing events timeline (from→to + reason + count); Claude Sessions panel (input/output/cache tokens, total cost, session count from `:8001`)
- **Dashboard Hub** (bottom) — 5 cards: AI Observability (Grafana), Claude Sessions (Grafana), Metrics Explorer (Prometheus), Log Explorer (Loki via Grafana Explore), Trace Explorer (Tempo via Grafana Explore)

**Design:**
- Palette: void `#06080f` · violet `#7c6ee0` · fuchsia `#d946ef` · teal/amber/red for state
- Fonts: `Rajdhani` (labels) + `JetBrains Mono` (values)
- SVG sparklines built via DOM API (`createElementNS`) — no innerHTML with data
- Service health via `fetch(url, {mode:'no-cors'})` — resolves = up, rejects = down
- Loki/Tempo hub links route to Grafana Explore (those services have no web UI at root)

**Auto-refreshes:** metrics every 5s · Claude sessions every 30s · service health every 15s

**Opens automatically on laptop login** via `~/.config/autostart/observability-portal.desktop`:
- 12-second autostart delay (lets Docker services come up first)
- Script waits up to 40s for Prometheus health before opening browser
- Starts HTTP server on port 8080 if not already running
- Opens in Brave browser (`/usr/bin/brave-browser`)

To start manually:
```bash
bash scripts/start-portal.sh
```

---

## Simulated Providers & Pricing

| Model | Provider | Input ($/1M tok) | Output ($/1M tok) |
|-------|----------|-------------------|--------------------|
| claude-sonnet-4-6 | Anthropic | $3.00 | $15.00 |
| claude-haiku-4-5  | Anthropic | $0.80 | $4.00  |
| gpt-4o            | OpenAI    | $2.50 | $10.00 |
| codex-mini-latest | OpenAI    | $1.50 | $6.00  |

---

## Verification Checklist

```bash
# Metrics live
curl -s http://localhost:8000/metrics | grep -E "ai_active_model|ai_model_switches"

# Policy engine firing
tail -f logs/ai_decisions.log

# Loki ingesting logs
curl -s "http://localhost:3100/loki/api/v1/query?query={job%3D%22ai_app%22}" | jq .

# Prometheus targets healthy
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[].health'

# Portal serving
curl -sf http://localhost:8080/portal/index.html | grep -c "MISSION"
```

---

## Level 5 Follow-ups (out of scope)

- Weighted / gradual rollout (A/B traffic splitting)
- Reinforcement learning cost optimizer
- Per-user / per-tenant routing policies
- Hard budget caps with enforcement (kill switch)
- Distributed version (Redis shared state, async workers)
- Real API key integration (currently all simulated)
