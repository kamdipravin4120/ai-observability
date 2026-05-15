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
                  "p95_latency", "error_rate", "cost_per_min", "blocked_switch_to",
                  "distribution", "from_distribution", "to_distribution", "daily_cost",
                  "tenant", "intent", "burn_rate"):
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
DAILY_COST_G   = Gauge(    "ai_daily_cost_usd",        "Accumulated daily cost")
ACTIVE_MODEL   = Gauge(    "ai_active_model",          "1 if category active",     ["category", "model"])
TRAFFIC_WEIGHT = Gauge(    "ai_traffic_weight",        "Weight of traffic per category", ["category"])
SWITCHES       = Counter(  "ai_model_switches_total",  "Model switch events",      ["from_model", "to_model", "reason"])
MODEL_REWARD   = Gauge(    "ai_model_reward",          "Current RL reward per category", ["category"])
TENANT_COST_G  = Gauge(    "ai_tenant_cost_usd",       "Daily cost per tenant",    ["tenant"])
VELOCITY_G     = Gauge(    "ai_spending_velocity_usd_hr", "Current spending velocity in USD/hr")

# ── Level 6: Governance State ──────────────────────────────────────────────────
TENANTS      = ["dev-team", "prod-api", "marketing-bot"]
INTENTS      = ["CRITICAL_CODE", "GENERAL_CHAT", "DATA_EXTRACTION"]
TENANT_COSTS = {t: 0.0 for t in TENANTS}
COST_HISTORY = [] # List of (timestamp, cost) for velocity tracking

# ── Router state ──────────────────────────────────────────────────────────────
CURRENT_DISTRIBUTION = {"default": 1.0}
LAST_SWITCH_TS   = 0.0
DAILY_COST       = 0.0
LAST_RESET_DAY   = time.strftime("%Y-%m-%d")
REWARDS          = {"default": 1.0, "fast": 1.0, "cheap": 1.0, "fallback": 1.0}
STATE_LOCK       = threading.Lock()

# ── Policy engine ─────────────────────────────────────────────────────────────
_DEFAULT_POLICIES = {
    "cooldown_seconds": 60,
    "daily_budget_usd": 10.0,
    "optimizer": {"enabled": False, "epsilon": 0.1},
    "models": {
        "default":   "claude-sonnet-4-6",
        "fast":      "claude-haiku-4-5",
        "cheap":     "codex-mini-latest",
        "fallback":  "gpt-4o",
        "emergency": "codex-mini-latest",
    },
    "policies": [
        {"name": "high_errors",   "condition": "error_rate > 0.03",    "distribution": {"fallback": 1.0}, "priority": 1},
        {"name": "high_latency",  "condition": "p95_latency > 2.0",    "distribution": {"fast": 0.3, "default": 0.7}, "priority": 2},
        {"name": "high_cost",     "condition": "cost_per_min > 0.1",   "distribution": {"cheap": 0.5, "default": 0.5}, "priority": 3},
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

def update_metrics_gauges(cfg, distribution):
    # Update active model gauge (1 if any weight > 0)
    for cat, model in cfg["models"].items():
        weight = distribution.get(cat, 0.0)
        ACTIVE_MODEL.labels(category=cat, model=model).set(1 if weight > 0 else 0)
        TRAFFIC_WEIGHT.labels(category=cat).set(weight)

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
    # RL Optimizer Mode
    opt_cfg = cfg.get("optimizer", {})
    if opt_cfg.get("enabled", False):
        epsilon = opt_cfg.get("epsilon", 0.1)
        if random.random() < epsilon:
            # Exploration: choose random category
            cats = [c for c in cfg["models"].keys() if c != "emergency"]
            chosen = random.choice(cats)
            return {chosen: 1.0}, "rl_exploration"
        else:
            # Exploitation: choose best category based on rewards
            with STATE_LOCK:
                best_cat = max(REWARDS, key=REWARDS.get)
            return {best_cat: 1.0}, "rl_exploitation"

    # Static Policy Mode (Fallback)
    for pol in sorted(cfg.get("policies", []), key=lambda x: x.get("priority", 99)):
        if check_condition(pol["condition"], metrics):
            # Support legacy 'action' key or new 'distribution'
            if "distribution" in pol:
                return pol["distribution"], pol["name"]
            return {pol["action"]: 1.0}, pol["name"]
    return {"default": 1.0}, "default"

def maybe_switch(metrics, cfg):
    global CURRENT_DISTRIBUTION, LAST_SWITCH_TS, DAILY_COST, LAST_RESET_DAY
    
    # 1. Check for budget reset
    today = time.strftime("%Y-%m-%d")
    if today != LAST_RESET_DAY:
        with STATE_LOCK:
            DAILY_COST = 0.0
            LAST_RESET_DAY = today
            print(f"[budget] reset for {today}")

    # 2. Check for budget kill switch (highest priority)
    budget_limit = cfg.get("daily_budget_usd", 10.0)
    if DAILY_COST >= budget_limit:
        new_distribution = {"emergency": 1.0}
        reason = "budget_limit_exceeded"
    else:
        new_distribution, reason = evaluate_policies(metrics, cfg)
    
    now = time.time()

    with STATE_LOCK:
        if new_distribution == CURRENT_DISTRIBUTION:
            decision_logger.info("no_change", extra={**metrics, "distribution": json.dumps(CURRENT_DISTRIBUTION), "daily_cost": DAILY_COST})
            return

        if now - LAST_SWITCH_TS < cfg.get("cooldown_seconds", 60):
            decision_logger.info("cooldown", extra={
                **metrics, "blocked_switch_to": json.dumps(new_distribution), "reason": reason,
                "distribution": json.dumps(CURRENT_DISTRIBUTION),
            })
            return

        # Log switches (simplified for distribution)
        old_models = [resolve_model(c, cfg) for c, w in CURRENT_DISTRIBUTION.items() if w > 0]
        new_models = [resolve_model(c, cfg) for c, w in new_distribution.items() if w > 0]
        
        # Increment switches for the primary change if possible, or just log
        SWITCHES.labels(from_model=str(old_models), to_model=str(new_models), reason=reason).inc()
        
        CURRENT_DISTRIBUTION = new_distribution
        LAST_SWITCH_TS   = now
        update_metrics_gauges(cfg, CURRENT_DISTRIBUTION)

        decision_logger.warning("switched", extra={
            **metrics,
            "from_distribution": json.dumps(CURRENT_DISTRIBUTION), 
            "to_distribution": json.dumps(new_distribution),
            "reason": reason
        })
        print(f"[policy] switched to distribution: {new_distribution}  (reason: {reason})")

def policy_loop(cfg):
    update_metrics_gauges(cfg, CURRENT_DISTRIBUTION)
    while True:
        try:
            metrics = get_metrics()
            
            # Level 6: Predictive Burn-Rate Analysis
            now = time.time()
            window = 300 # 5 min sliding window for faster simulation feedback
            with STATE_LOCK:
                global COST_HISTORY
                COST_HISTORY = [item for item in COST_HISTORY if item[0] > (now - window)]
                sum_cost = sum(item[1] for item in COST_HISTORY)
            
            velocity = (sum_cost / window) * 3600 # Scale to USD/hr
            VELOCITY_G.set(velocity)
            metrics["burn_rate"] = velocity
            
            maybe_switch(metrics, cfg)
        except Exception as e:
            print(f"[policy] error: {e}")
        time.sleep(10)

# ── Request simulation ────────────────────────────────────────────────────────
def update_reward(category, latency, cost, error):
    global REWARDS
    penalty = 10.0 if error else 0.0
    # Higher reward for lower latency, lower cost, and no errors
    # latency is in seconds, cost is in USD (e.g. 0.001)
    # Normalized reward: 100 / (latency_factor + cost_factor + penalty)
    current_reward = 100.0 / (latency * 2.0 + cost * 5000 + penalty + 0.1)
    
    with STATE_LOCK:
        # Exponential moving average (alpha=0.1)
        REWARDS[category] = REWARDS.get(category, 1.0) * 0.9 + current_reward * 0.1
        MODEL_REWARD.labels(category=category).set(REWARDS[category])

def simulate_request(tracer, cfg):
    tenant = random.choice(TENANTS)
    intent = random.choice(INTENTS)
    
    with STATE_LOCK:
        dist = CURRENT_DISTRIBUTION
    
    # Semantic Override (Level 6)
    overrides = cfg.get("semantic_overrides", {})
    if intent in overrides:
        dist = overrides[intent].get("distribution", dist)
    
    # Probabilistic selection
    categories = list(dist.keys())
    weights    = list(dist.values())
    current_cat = random.choices(categories, weights=weights, k=1)[0]
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
        span.set_attribute("tenant", tenant)
        span.set_attribute("intent", intent)
        span.set_attribute("tokens.input", tokens_in)
        span.set_attribute("tokens.output", tokens_out)
        span.set_attribute("error", is_error)

        start_t = time.time()
        time.sleep(random.uniform(*pcfg["latency"]))
        latency = time.time() - start_t
        LATENCY.labels(**labels).observe(latency)

        if is_error:
            ERRORS.labels(**labels).inc()
            span.set_status(StatusCode.ERROR, "simulated failure")
            update_reward(current_cat, latency, 0.0, True)
            request_logger.error("request_error", extra={
                "provider": provider, "model": model, "category": current_cat, "error": True,
                "tenant": tenant, "intent": intent
            })
            return

        cost = tokens_in * pcfg["input_cost"] + tokens_out * pcfg["output_cost"]
        TOKENS_INPUT.labels(**labels).inc(tokens_in)
        TOKENS_OUTPUT.labels(**labels).inc(tokens_out)
        COST.labels(**labels).inc(cost)
        
        with STATE_LOCK:
            global DAILY_COST
            DAILY_COST += cost
            DAILY_COST_G.set(DAILY_COST)
            TENANT_COSTS[tenant] += cost
            TENANT_COST_G.labels(tenant=tenant).set(TENANT_COSTS[tenant])
            COST_HISTORY.append((time.time(), cost))

        span.set_attribute("cost.usd", cost)
        update_reward(current_cat, latency, cost, False)
        request_logger.info("request_ok", extra={
            "provider": provider, "model": model, "category": current_cat,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost": round(cost, 6), "error": False, "daily_cost": round(DAILY_COST, 4),
            "tenant": tenant, "intent": intent
        })

# ── Main ──────────────────────────────────────────────────────────────────────
def run(cfg):
    while True:
        simulate_request(tracer, cfg)
        time.sleep(random.uniform(0.5, 2.0))

if __name__ == "__main__":
    cfg = load_policies()
    print("Starting metrics server on port 8002...")
    start_http_server(8002)

    t = threading.Thread(target=policy_loop, args=(cfg,), daemon=True)
    t.start()
    print("Policy engine started (10s interval)")

    run(cfg)
