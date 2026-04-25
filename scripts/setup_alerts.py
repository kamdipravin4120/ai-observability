#!/usr/bin/env python3
"""
Provisions 3 Grafana alert rules for the AI Observability stack.
Uses Grafana provisioning API (v1) — compatible with Grafana 9+.

Usage:
    python3 scripts/setup_alerts.py --url http://localhost:3007 --user admin --password <pass>
"""
import argparse
import json
import sys
import requests

FOLDER_UID   = "ai-alerts"
FOLDER_TITLE = "AI Alerts"
GROUP_NAME   = "AI Observability"

RULES = [
    {
        "uid":       "ai-high-error-rate",
        "title":     "High Error Rate (>2%)",
        "for":       "2m",
        "labels":    {"severity": "critical"},
        "annotations": {
            "summary":     "AI error rate exceeded 2%",
            "description": "sum(rate(ai_errors_total[5m])) / sum(rate(ai_requests_total[5m])) > 0.02 for 2 minutes",
        },
        "expr":      "sum(rate(ai_errors_total[5m])) / sum(rate(ai_requests_total[5m]))",
        "threshold": 0.02,
    },
    {
        "uid":       "ai-high-latency-p95",
        "title":     "High P95 Latency (>2s)",
        "for":       "2m",
        "labels":    {"severity": "warning"},
        "annotations": {
            "summary":     "AI P95 latency exceeded 2s",
            "description": "P95 latency above 2 seconds for 2 minutes",
        },
        "expr":      "histogram_quantile(0.95, sum by (le) (rate(ai_latency_seconds_bucket[5m])))",
        "threshold": 2.0,
    },
    {
        "uid":       "ai-cost-spike",
        "title":     "Cost Spike (>$0.10/min)",
        "for":       "2m",
        "labels":    {"severity": "warning"},
        "annotations": {
            "summary":     "AI cost exceeded $0.10/min",
            "description": "Aggregate cost rate above $0.10 per minute for 2 minutes",
        },
        "expr":      "sum(rate(ai_cost_usd_total[5m])) * 60",
        "threshold": 0.10,
    },
]


def build_payload(rule, prom_uid):
    return {
        "uid":          rule["uid"],
        "title":        rule["title"],
        "ruleGroup":    GROUP_NAME,
        "folderUID":    FOLDER_UID,
        "condition":    "threshold",
        "noDataState":  "NoData",
        "execErrState": "Error",
        "for":          rule["for"],
        "labels":       rule["labels"],
        "annotations":  rule["annotations"],
        "data": [
            {
                "refId":             "A",
                "datasourceUid":     prom_uid,
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {
                    "expr":    rule["expr"],
                    "refId":   "A",
                    "instant": True,
                },
            },
            {
                "refId":             "threshold",
                "datasourceUid":     "-100",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {
                    "type":  "classic_conditions",
                    "refId": "threshold",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [rule["threshold"]]},
                            "operator":  {"type": "and"},
                            "query":     {"params": ["A"]},
                            "reducer":   {"type": "last", "params": []},
                            "type":      "query",
                        }
                    ],
                },
            },
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Provision Grafana alert rules")
    parser.add_argument("--url",      required=True, help="Grafana base URL")
    parser.add_argument("--user",     required=True, help="Grafana username")
    parser.add_argument("--password", required=True, help="Grafana password")
    args = parser.parse_args()

    s = requests.Session()
    s.auth = (args.user, args.password)
    base = args.url.rstrip("/")

    # 1. Prometheus datasource UID
    r = s.get(f"{base}/api/datasources")
    if r.status_code != 200:
        print(f"ERROR: GET /api/datasources → {r.status_code}: {r.text}")
        sys.exit(1)
    prom_uid = next((d["uid"] for d in r.json() if d["type"] == "prometheus"), None)
    if not prom_uid:
        print("ERROR: No Prometheus datasource in Grafana")
        sys.exit(1)
    print(f"Prometheus UID: {prom_uid}")

    # 2. Create folder
    r = s.post(f"{base}/api/folders", json={"title": FOLDER_TITLE, "uid": FOLDER_UID})
    if r.status_code in (200, 201, 409, 412):
        print(f"Folder '{FOLDER_TITLE}' ready")
    else:
        print(f"WARNING: folder → {r.status_code}: {r.text}")

    # 3. Provision each rule (upsert: delete if exists, then create)
    print()
    for rule in RULES:
        s.delete(f"{base}/api/v1/provisioning/alert-rules/{rule['uid']}")
        payload = build_payload(rule, prom_uid)
        r = s.post(f"{base}/api/v1/provisioning/alert-rules", json=payload)
        if r.status_code in (200, 201):
            print(f"  [{rule['labels']['severity'].upper()}] {rule['title']}")
        else:
            print(f"  ERROR {rule['title']} → {r.status_code}: {r.text}")


if __name__ == "__main__":
    main()
