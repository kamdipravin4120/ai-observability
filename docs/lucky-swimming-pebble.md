# Plan: AI Observability — Closed-Loop Control System (Level 4)

## Context

Stack at Level 2 today (metrics + dashboards, no feedback). User wants Level 4: dashboards drive a control loop that automatically routes traffic to a different model when latency, cost, or error-rate thresholds are crossed — no human in the loop.

Architecture target:

```
Prometheus ──► Policy Engine ──► Router ──► Model call
    ▲                                │
    └────── feedback ◄───────────────┘
```

Level 3 prerequisites (logs + traces + alerts + thresholds) are folded in since the control loop needs them for audit and safety.

## Key Design Decisions

1. **Single process.** Policy engine runs as a background thread inside `ai_app.py`. Avoids IPC; state is a shared global protected by a lock. Matches the user's code sketches.
2. **Model categories, not raw names.** App asks for `default | fast | cheap | fallback`; a `models:` mapping in `policies.yml` resolves category → actual model id.
3. **Hysteresis by cooldown.** After any switch, lock the choice for `cooldown_seconds` (default 60) to prevent flapping.
4. **Audit everything.** Every decision (including "no change") logged to `logs/ai_decisions.log` as JSON → Loki. Dashboard shows the timeline.
5. **Safe condition parser — no code execution.** Policy conditions are constrained to `<metric_name> <op> <number>` (ops: `>`, `>=`, `<`, `<=`, `==`). A tiny manual parser handles them; no `eval`, no third-party expression library.
6. **Existing metrics preserved.** `ai_requests_total{provider,model}` etc. unchanged. Two new metrics added: `ai_active_model{category,model}` (gauge), `ai_model_switches_total{from_model,to_model,reason}` (counter).

## Files to Modify / Create

| File | Action |
|------|--------|
| [ai_app.py](/home/pk/Work/observability/ai_app.py) | major rewrite — OTEL + logging + policy engine + router |
| `policies.yml` | create — declarative rules + model category mapping |
| [promtail-config.yml](/home/pk/Work/observability/promtail-config.yml) | modify — scrape `ai_app.log` and `ai_decisions.log` |
| [docker-compose.yml](/home/pk/Work/observability/docker-compose.yml) | modify — mount `./logs` in promtail |
| [grafana-dashboard.json](/home/pk/Work/observability/grafana-dashboard.json) | modify — add control panels, threshold bands, Loki panels |
| `setup_alerts.py` | create — Grafana API script provisioning 3 alert rules |

## Change Details

### 1. `ai_app.py` — rewrite

Sections, top to bottom:

1. **Imports + config load** — `yaml` if available else hard-coded defaults; OTEL trace/SDK/OTLPSpanExporter; stdlib `threading`, `logging`, `json`, `requests`, `re`.

2. **Constants**
   ```python
   POLICIES_PATH = "policies.yml"
   PROM_URL = "http://localhost:9090/api/v1/query"
   ```

3. **Structured JSON loggers** — two:
   - `request_logger` → `logs/ai_app.log`
   - `decision_logger` → `logs/ai_decisions.log`
   JSON formatter emits `ts, level, msg, ...extras`.

4. **OTEL setup** — `TracerProvider(resource=Resource.create({"service.name":"ai_app"}))`, `OTLPSpanExporter(endpoint="localhost:4317", insecure=True)`, `BatchSpanProcessor`.

5. **Prometheus metrics** — existing 6, plus:
   - `ai_active_model = Gauge("ai_active_model", "1 if category routes to this model", ["category","model"])`
   - `ai_model_switches_total = Counter("ai_model_switches_total", "Model switches", ["from_model","to_model","reason"])`

6. **Provider config** (pricing/latency) — unchanged dict.

7. **Router state**
   ```python
   CURRENT_CATEGORY = "default"
   LAST_SWITCH_TS = 0.0
   STATE_LOCK = threading.Lock()
   ```

8. **`load_policies()`** — parse `policies.yml`; fall back to embedded defaults if file missing. Returns `{cooldown, policies:[...], models:{default,fast,cheap,fallback}}`.

9. **`parse_condition(expr)`** — regex `^\s*(\w+)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$` → `(var, op, threshold)`. Raises `ValueError` on malformed input. Unit-testable.

10. **`eval_condition(cond_tuple, metrics)`** — dispatch on op: `>` returns `metrics[var] > threshold`, etc. Returns `False` if var missing.

11. **`resolve_model(category, cfg)`** — returns `cfg["models"][category]`.

12. **`simulate_request(category, cfg)`**
    - Resolves model; looks up provider/pricing from PROVIDERS
    - Opens OTEL span `{provider}/{model}` with attributes `category`, `provider`, `model`, `tokens.input`, `tokens.output`
    - Increments `ai_requests_total{provider,model}`
    - 10% simulated error → `span.set_status(StatusCode.ERROR)`, `request_logger.error(...)` with structured fields
    - Success → update token/cost counters, `request_logger.info(...)`

13. **`query_prometheus(expr)`** — GET PROM_URL with `params={"query":expr}`; safely return `float(data["data"]["result"][0]["value"][1])` or `0.0`.

14. **`get_metrics()`** — dict:
    ```
    p95_latency  = histogram_quantile(0.95, sum by (le) (rate(ai_latency_seconds_bucket[5m])))
    error_rate   = sum(rate(ai_errors_total[5m])) / sum(rate(ai_requests_total[5m]))
    cost_per_min = sum(rate(ai_cost_usd_total[5m])) * 60
    ```

15. **`evaluate_policies(metrics, cfg)`** — iterate policies sorted by `priority`; return `(category, policy_name)` on first true condition. Fallback to `("default","default")`.

16. **`maybe_switch(metrics, cfg)`** — with `STATE_LOCK`:
    - same category → `decision_logger.info("no_change", extra={metrics,...})`
    - within cooldown → `decision_logger.info("cooldown", extra={blocked_switch_to, metrics,...})`
    - otherwise switch: inc `ai_model_switches_total`, update globals, refresh `ai_active_model` gauge, `decision_logger.warning("switched", extra={from_model,to_model,reason,metrics})`

17. **`update_active_model_gauge(cfg)`** — set all 4 category/model series to 0, set current one to 1.

18. **`policy_loop(cfg)`** — background thread: every 10s → `get_metrics()` → `maybe_switch()`. First tick also populates the gauge.

19. **`main_loop(cfg)`** — every second, fire one `simulate_request(CURRENT_CATEGORY, cfg)` per source (four "callers" simulate traffic; they all use whatever category is currently active).

20. **`__main__`** — load policies, `start_http_server(8000)`, spawn daemon policy thread, run main loop.

### 2. `policies.yml` — new

```yaml
cooldown_seconds: 60

models:
  default:  claude-sonnet-4-6
  fast:     claude-haiku-4-5
  cheap:    codex-mini-latest
  fallback: gpt-4o

policies:
  - name: high_errors
    condition: "error_rate > 0.03"
    action: fallback
    priority: 1
  - name: high_latency
    condition: "p95_latency > 2.0"
    action: fast
    priority: 2
  - name: high_cost
    condition: "cost_per_min > 0.1"
    action: cheap
    priority: 3
```

(No explicit "default" entry needed — `evaluate_policies` falls back to `default` when no condition matches.)

### 3. `promtail-config.yml` — modify

Append two scrape jobs:

```yaml
  - job_name: ai_app_requests
    static_configs:
      - targets: [localhost]
        labels:
          job: ai_app
          stream: requests
          __path__: /app/logs/ai_app.log
    pipeline_stages:
      - json:
          expressions:
            level: level
            provider: provider
            model: model
      - labels:
          level:
          provider:
          model:

  - job_name: ai_app_decisions
    static_configs:
      - targets: [localhost]
        labels:
          job: ai_app
          stream: decisions
          __path__: /app/logs/ai_decisions.log
    pipeline_stages:
      - json:
          expressions:
            level: level
            reason: reason
            from_model: from_model
            to_model: to_model
      - labels:
          level:
          reason:
```

### 4. `docker-compose.yml` — modify

Under `promtail.volumes`, append:
```yaml
      - ./logs:/app/logs
```

### 5. `grafana-dashboard.json` — modify

- Add `DS_LOKI` to `__inputs`.
- Threshold bands (`custom.thresholdsStyle.mode = "area"`) on panels 6 (error rate, amber@0.01, red@0.03), 7 (P95+P50, amber@1.5, red@2), 8 (avg latency, amber@1.5, red@2).
- Panel 15 (table/stat, y=45, x=0, w=6, h=5): **Active Model** — `ai_active_model == 1`
- Panel 16 (timeseries, y=45, x=6, w=6, h=5): **Model Switches / hr** — `rate(ai_model_switches_total[5m]) * 3600` by `reason`
- Panel 17 (timeseries, y=45, x=12, w=12, h=5): **Cost per Request** — `rate(ai_cost_usd_total{provider=~"$provider",model=~"$model"}[1m]) / rate(ai_requests_total{provider=~"$provider",model=~"$model"}[1m])` per model with neon overrides
- Panel 18 (logs, y=50, x=0, w=12, h=10): **Decision Timeline** — Loki `{job="ai_app", stream="decisions"}`
- Panel 19 (logs, y=50, x=12, w=12, h=10): **Error Logs** — Loki `{job="ai_app", stream="requests", level="ERROR"}`

### 6. `setup_alerts.py` — new

Standalone script, depends only on `requests`:

- Args: `--url`, `--user`, `--password` (required)
- GET `{url}/api/datasources` → first `type=prometheus` → capture `uid`
- POST `{url}/api/folders` body `{"title":"AI Alerts","uid":"ai-alerts"}`; tolerate 409/412
- POST `{url}/api/ruler/grafana/api/v1/rules/AI%20Alerts` with rule group `AI Observability`. Each rule uses the v1 schema (two data nodes: metric query + classic_condition):
  - `high-error-rate` — `sum(rate(ai_errors_total[5m])) / sum(rate(ai_requests_total[5m]))` > 0.02, for 2m, severity=critical
  - `high-latency-p95` — `histogram_quantile(0.95, sum by (le) (rate(ai_latency_seconds_bucket[5m])))` > 2, for 2m, severity=warning
  - `cost-spike` — `sum(rate(ai_cost_usd_total[5m])) * 60` > 0.1, for 2m, severity=warning
- Print concise status per rule

## Execution Order

1. Rewrite `ai_app.py`
2. Create `policies.yml`
3. Patch `promtail-config.yml`
4. Patch `docker-compose.yml`
5. Stop old `ai_app.py` (kill pid on :8000), start new one
6. `docker compose restart promtail` (pick up new mount + config)
7. Patch `grafana-dashboard.json`
8. Re-import dashboard in Grafana UI
9. Write `setup_alerts.py`
10. Tell user to run it with their Grafana credentials
11. Save session memory (project progress, design decisions) — separate step after plan approval since memory writes are blocked in plan mode

## Verification

1. **Metrics**: `curl :8000/metrics | grep -E "ai_active_model|ai_model_switches"` shows the new series
2. **Traces**: Grafana → Explore → Tempo → Service `ai_app` → spans show `category` attribute
3. **Request logs**: Grafana → Explore → Loki → `{job="ai_app", stream="requests"}` streams JSON
4. **Decision logs**: `{job="ai_app", stream="decisions"}` — see `no_change`, `cooldown`, `switched`
5. **Control loop**: temporarily bump `random.random() < 0.5` (instead of 0.1) in `simulate_request`; within ~30s see `switched → fallback` decision and `ai_active_model{model="gpt-4o"}=1`; revert after.
6. **Hysteresis**: after a switch, second threshold crossing within 60s logs `cooldown` not `switched`
7. **Dashboard**: 19 panels populated, Decision Timeline scrolls, threshold bands visible
8. **Alerts**: after `setup_alerts.py`, 3 rules appear under Alerting → Alert rules → AI Alerts

## Safety Notes

- Condition parser is whitelist-only (`variable OP number`). No code execution path.
- Daemon thread — if main loop crashes, policy thread dies with it. Acceptable for simulator; production needs supervisor.
- Prometheus empty on first tick → `get_metrics()` returns 0.0 → stays on `default`. Correct.
- Cooldown prevents pathological flapping but means the system is slow to react (60s worst case). Tunable in `policies.yml`.

## Out of Scope (Level 5 follow-ups)

- Weighted / gradual rollout (A/B). Hook is present via `CURRENT_CATEGORY` but initial impl is hard switch.
- Reinforcement learning optimizer
- Per-user / per-tenant routing
- Hard budget caps with enforcement
- Distributed version (Redis, async workers)

## Pending User Actions After Approval

1. `/compact` — this is a CLI slash command only the user can issue; I can't run it from the agent side.
2. After plan approval, I'll save these to memory (currently blocked by plan mode):
   - Project overview (stack components, purpose)
   - Progress: Level 2 → 4 upgrade in progress
   - Design decisions (single-process, category-based routing, cooldown hysteresis, whitelist condition parser)
