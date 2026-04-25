# Network Ports & Services

## Observability Stack (Docker Compose)

| Service | Container | Host Port | Container Port | URL |
|---------|-----------|-----------|----------------|-----|
| Grafana | observability-grafana-1 | 3007 | 3000 | http://localhost:3007 |
| Prometheus | observability-prometheus-1 | 9090 | 9090 | http://localhost:9090 |
| Loki | observability-loki-1 | 3100 | 3100 | http://localhost:3100 |
| Tempo | observability-tempo-1 | 3200 | 3200 | http://localhost:3200 |
| OTEL Collector (gRPC) | observability-otel-collector-1 | 4317 | 4317 | grpc://localhost:4317 |
| OTEL Collector (HTTP) | observability-otel-collector-1 | 4318 | 4318 | http://localhost:4318 |
| Node Exporter | observability-node_exporter-1 | 9100 | 9100 | http://localhost:9100/metrics |
| Promtail | observability-promtail-1 | — | — | (no exposed port) |

## AI App (Bare Metal)

| Service | Process | Port | URL |
|---------|---------|------|-----|
| ai_app.py metrics | python3 (pid 2259063) | 8000 | http://localhost:8000/metrics |

## Other Running Services (Not This Project)

| Service | Container / Process | Host Port | Notes |
|---------|-------------------|-----------|-------|
| Open WebUI | open-webui | 3000 | ghcr.io/open-webui/open-webui |
| Ollama | bare metal (/usr/local/bin/ollama) | 11434 (localhost only) | LLM inference server |
| PostgreSQL | job-automation-ai-postgres-1 | 5432 (localhost only) | postgres:16-alpine |
| Redis | job-automation-ai-redis-1 | 6379 (localhost only) | redis:7-alpine |

## Port Conflict Warning

Port `3000` used by **both**:
- Open WebUI (host `0.0.0.0:3000 → container:8080`)
- Grafana docker-compose config maps host `3007 → container:3000` (already adjusted to avoid conflict)

No active conflict — Grafana exposed on `3007`, not `3000`.
