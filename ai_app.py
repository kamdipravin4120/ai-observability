import os
import re
import json
import time
import random
import logging
import threading
import requests

from prometheus_client import start_http_server, Counter, Histogram, Gauge

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import StatusCode

# ── Constants ─────────────────────────────────────────────────────────────────
POLICIES_PATH = "config/policies.yml"
PROM_URL      = "http://localhost:9090/api/v1/query"

# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

class _JSONFormatter(logging.Formatter):
    def format(self, record):
        data = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "msg": record.getMessage()}
        for k in ("provider", "model", "category", "latency", "tokens_in", "tokens_out",
                  "cost", "error", "reason", "from_model", "to_model",
                  "p95_latency", "error_rate", "cost_per_min", "blocked_switch_to"):
            if hasattr(record, k):
                data[k] = getattr(record, k)
        return json.dumps(data)

def _make_logger(name, path):
    h = logging.FileHandler(path)
    h.setFormatter(_JSONFormatter())
    lg = logging.getLogger(name)
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    return lg

request_logger  = _make_logger("ai_requests",  "logs/ai_app.log")
decision_logger = _make_logger("ai_decisions", "logs/ai_decisions.log")

# ── OTEL tracing ──────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "ai_app", "service.version": "2.0"})
_provider = TracerProvider(resource=_resource)
_exporter = OTLPSpanExporter(endpoint="localhost:4317", insecure=True)
_provider.add_span_processor(BatchSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("ai_app")

# ── Provider config: pricing ($/token) + latency range (s) ───────────────────
PROVIDERS = {
    "anthropic": {
        "claude-sonnet-4-6": {"input_cost": 3e-6,    "output_cost": 15e-6,   "latency": (0.5, 3.0)},
        "claude-haiku-4-5":  {"input_cost": 0.25e-6, "output_cost": 1.25e-6, "latency": (0.2, 1.5)},
    },
    "openai": {
        "gpt-4o":            {"input_cost": 2.5e-6,  "output_cost": 10e-6,   "latency": (0.3, 2.0)},
        "codex-mini-latest": {"input_cost": 1.5e-6,  "output_cost": 6e-6,    "latency": (0.2, 1.5)},
    },
}

# Map model id → provider
_MODEL_TO_PROVIDER = {m: p for p, ms in PROVIDERS.items() for m in ms}

# ── Prometheus metrics ────────────────────────────────────────────────────────
LABELS = ["provider", "model"]

REQUEST_COUNT  = Counter(  "ai_requests_total",       "Total AI requests",        LABELS)
LATENCY        = Histogram("ai_latency_seconds",       "Request latency",          LABELS)
ERRORS         = Counter(  "ai_errors_total",          "Request errors",           LABELS)
TOKENS_INPUT   = Counter(  "ai_tokens_input_total",    "Input tokens consumed",    LABELS)
TOKENS_OUTPUT  = Counter(  "ai_tokens_output_total",   "Output tokens consumed",   LABELS)
COST           = Counter(  "ai_cost_usd_total",        "Cost in USD",              LABELS)
ACTIVE_MODEL   = Gauge(    "ai_active_model",          "1 if category active",     ["category", "model"])
SWITCHES       = Counter(  "ai_model_switches_total",  "Model switch events",      ["from_model", "to_model", "reason"])

# ── Router state ──────────────────────────────────────────────────────────────
CURRENT_CATEGORY = "default"
LAST_SWITCH_TS   = 0.0
STATE_LOCK       = threading.Lock()

# ── Policy engine ─────────────────────────────────────────────────────────────
_DEFAULT_POLICIES = {
    "cooldown_seconds": 60,
    "models": {
        "default":  "claude-sonnet-4-6",
        "fast":     "claude-haiku-4-5",
        "cheap":    "codex-mini-latest",
        "fallback": "gpt-4o",
    },
    "policies": [
        {"name": "high_errors",   "condition": "error_rate > 0.03",    "action": "fallback", "priority": 1},
        {"name": "high_latency",  "condition": "p95_latency > 2.0",    "action": "fast",     "priority": 2},
        {"name": "high_cost",     "condition": "cost_per_min > 0.1",   "action": "cheap",    "priority": 3},
    ],
}

def load_policies():
    try:
        import yaml
        with open(POLICIES_PATH) as f:
            cfg = yaml.safe_load(f)
        print(f"Loaded policies from {POLICIES_PATH}")
        return cfg
    except Exception:
        print(f"Using default policies ({POLICIES_PATH} not found or yaml missing)")
        return _DEFAULT_POLICIES

_COND_RE = re.compile(r"^\s*(\w+)\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")

def parse_condition(expr):
    m = _COND_RE.match(expr)
    if not m:
        raise ValueError(f"Invalid policy condition: {expr!r}")
    return m.group(1), m.group(2), float(m.group(3))

def check_condition(expr, metrics):
    try:
        var, op, threshold = parse_condition(expr)
        val = metrics.get(var, 0.0)
        return {">": val > threshold, ">=": val >= threshold,
                "<": val < threshold, "<=": val <= threshold,
                "==": val == threshold, "!=": val != threshold}[op]
    except Exception:
        return False

def resolve_model(category, cfg):
    return cfg["models"].get(category, cfg["models"]["default"])

def update_active_model_gauge(cfg):
    for cat, model in cfg["models"].items():
        ACTIVE_MODEL.labels(category=cat, model=model).set(
            1 if cat == CURRENT_CATEGORY else 0
        )

# ── Prometheus queries ────────────────────────────────────────────────────────
def query_prometheus(expr):
    try:
        r = requests.get(PROM_URL, params={"query": expr}, timeout=3)
        result = r.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0
    except Exception:
        return 0.0

def get_metrics():
    return {
        "p95_latency":  query_prometheus(
            "histogram_quantile(0.95, sum by (le) (rate(ai_latency_seconds_bucket[5m])))"),
        "error_rate":   query_prometheus(
            "sum(rate(ai_errors_total[5m])) / sum(rate(ai_requests_total[5m]))"),
        "cost_per_min": query_prometheus(
            "sum(rate(ai_cost_usd_total[5m])) * 60"),
    }

# ── Policy evaluation + switching ─────────────────────────────────────────────
def evaluate_policies(metrics, cfg):
    for pol in sorted(cfg["policies"], key=lambda p: p["priority"]):
        if check_condition(pol["condition"], metrics):
            return pol["action"], pol["name"]
    return "default", "default"

def maybe_switch(metrics, cfg):
    global CURRENT_CATEGORY, LAST_SWITCH_TS
    new_category, reason = evaluate_policies(metrics, cfg)
    now = time.time()

    with STATE_LOCK:
        if new_category == CURRENT_CATEGORY:
            decision_logger.info("no_change", extra={**metrics, "category": CURRENT_CATEGORY})
            return

        if now - LAST_SWITCH_TS < cfg["cooldown_seconds"]:
            decision_logger.info("cooldown", extra={
                **metrics, "blocked_switch_to": new_category, "reason": reason,
                "category": CURRENT_CATEGORY,
            })
            return

        old_model = resolve_model(CURRENT_CATEGORY, cfg)
        new_model = resolve_model(new_category, cfg)

        SWITCHES.labels(from_model=old_model, to_model=new_model, reason=reason).inc()
        CURRENT_CATEGORY = new_category
        LAST_SWITCH_TS   = now
        update_active_model_gauge(cfg)

        decision_logger.warning("switched", extra={
            **metrics,
            "from_model": old_model, "to_model": new_model,
            "reason": reason, "category": new_category,
        })
        print(f"[policy] switched {old_model} → {new_model}  (reason: {reason})")

def policy_loop(cfg):
    update_active_model_gauge(cfg)
    while True:
        try:
            metrics = get_metrics()
            maybe_switch(metrics, cfg)
        except Exception as e:
            print(f"[policy] error: {e}")
        time.sleep(10)

# ── Request simulation ────────────────────────────────────────────────────────
def simulate_request(cfg):
    with STATE_LOCK:
        current_cat = CURRENT_CATEGORY
    model    = resolve_model(current_cat, cfg)
    provider = _MODEL_TO_PROVIDER.get(model, "unknown")
    pcfg     = PROVIDERS.get(provider, {}).get(model, {"input_cost": 0, "output_cost": 0, "latency": (0.5, 1.5)})

    labels     = {"provider": provider, "model": model}
    tokens_in  = random.randint(100, 2000)
    tokens_out = random.randint(50, 500)
    is_error   = random.random() < 0.1

    REQUEST_COUNT.labels(**labels).inc()

    with tracer.start_as_current_span(f"{provider}/{model}") as span:
        span.set_attribute("provider", provider)
        span.set_attribute("model", model)
        span.set_attribute("category", current_cat)
        span.set_attribute("tokens.input", tokens_in)
        span.set_attribute("tokens.output", tokens_out)
        span.set_attribute("error", is_error)

        with LATENCY.labels(**labels).time():
            time.sleep(random.uniform(*pcfg["latency"]))

        if is_error:
            ERRORS.labels(**labels).inc()
            span.set_status(StatusCode.ERROR, "simulated failure")
            request_logger.error("request_failed", extra={
                "provider": provider, "model": model, "category": current_cat, "error": True,
            })
            return

        cost = tokens_in * pcfg["input_cost"] + tokens_out * pcfg["output_cost"]
        TOKENS_INPUT.labels(**labels).inc(tokens_in)
        TOKENS_OUTPUT.labels(**labels).inc(tokens_out)
        COST.labels(**labels).inc(cost)
        span.set_attribute("cost.usd", cost)
        request_logger.info("request_ok", extra={
            "provider": provider, "model": model, "category": current_cat,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost": round(cost, 6), "error": False,
        })

# ── Main ──────────────────────────────────────────────────────────────────────
def run(cfg):
    while True:
        simulate_request(cfg)
        time.sleep(1)

if __name__ == "__main__":
    cfg = load_policies()
    print("Starting metrics server on port 8000...")
    start_http_server(8000)

    t = threading.Thread(target=policy_loop, args=(cfg,), daemon=True)
    t.start()
    print("Policy engine started (10s interval)")

    run(cfg)
