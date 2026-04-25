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
5. **Safe condition parser — no code execution.** Policy conditions are constrained to `<metric_name> <op> <number>` (ops: `>`, `>=`, `<`, `<=`, `==`). A tiny manual parser handles them.
6. **Existing metrics preserved.** `ai_requests_total{provider,model}` etc. unchanged. Two new metrics added: `ai_active_model{category,model}` (gauge), `ai_model_switches_total{from_model,to_model,reason}` (counter).

## Files Modified / Created

| File | Action |
|------|--------|
| `ai_app.py` | major rewrite — OTEL + logging + policy engine + router |
| `policies.yml` | created — declarative rules + model category mapping |
| `promtail-config.yml` | modified — scrape `ai_app.log` and `ai_decisions.log` |
| `docker-compose.yml` | modified — mount `./logs` in promtail |
| `grafana-dashboard.json` | modified — control panels, threshold bands, Loki panels |
| `setup_alerts.py` | created — Grafana API script provisioning 3 alert rules |

## Policies (policies.yml)

```yaml
cooldown_seconds: 60

models:
  default:  claude-sonnet-4-6
  fast:     claude-haiku-4-5
  cheap:    codex-mini-latest
  fallback: gpt-4o

policies:
  - name: high_errors   condition: error_rate > 0.03    action: fallback  priority: 1
  - name: high_latency  condition: p95_latency > 2.0    action: fast      priority: 2
  - name: high_cost     condition: cost_per_min > 0.1   action: cheap     priority: 3
```

## New Prometheus Metrics

- `ai_active_model{category, model}` — Gauge, 1 for currently active model
- `ai_model_switches_total{from_model, to_model, reason}` — Counter per switch event

## New Log Files (→ Loki)

- `logs/ai_app.log` — every request (JSON): ts, level, provider, model, category, latency, tokens, cost, error
- `logs/ai_decisions.log` — every policy evaluation (JSON): ts, level, msg (no_change/cooldown/switched), metrics snapshot, reason

## Dashboard Panels Added

- Panel 15: Active Model (stat)
- Panel 16: Model Switches / hr (timeseries)
- Panel 17: Cost per Request (timeseries, by model)
- Panel 18: Decision Timeline (Loki logs)
- Panel 19: Error Logs (Loki logs)
- Threshold bands on error rate (>1% amber, >3% red) + latency (>1.5s amber, >2s red)

## User Actions Required

1. Re-import `grafana-dashboard.json` in Grafana (delete old first)
2. Run alerts setup:
   ```
   python3 setup_alerts.py --url http://localhost:3007 --user <user> --password <pass>
   ```
3. Restart promtail to pick up logs mount:
   ```
   docker compose restart promtail
   ```

## Verification

```bash
# New metrics present
curl -s http://localhost:8000/metrics | grep -E "ai_active_model|ai_model_switches"

# Decisions being logged
tail -f logs/ai_decisions.log

# Traces in Tempo
# Grafana → Explore → Tempo → Service Name: ai_app
```

## Level 5 Follow-ups (out of scope)

- Weighted / gradual rollout (A/B)
- Reinforcement learning optimizer
- Per-user / per-tenant routing
- Hard budget caps with enforcement
- Distributed version (Redis + async workers)
