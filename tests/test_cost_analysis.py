import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

import cost_analysis as ca  # noqa: E402


def ce_period(day, services):
    return {
        "TimePeriod": {"Start": day, "End": day},
        "Groups": [
            {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}}}
            for name, cost in services.items()
        ],
    }


# The real-world case: EKS jumps from $0.12/day to $17.12 in one day.
EKS_SPIKE = [
    ce_period("2026-11-01", {"Amazon Elastic Kubernetes Service": 0.12, "Amazon S3": 0.05}),
    ce_period("2026-11-02", {"Amazon Elastic Kubernetes Service": 0.12, "Amazon S3": 0.05}),
    ce_period("2026-11-03", {"Amazon Elastic Kubernetes Service": 0.12, "Amazon S3": 0.06}),
    ce_period("2026-11-04", {"Amazon Elastic Kubernetes Service": 0.12, "Amazon S3": 0.05}),
    ce_period("2026-11-05", {"Amazon Elastic Kubernetes Service": 0.12, "Amazon S3": 0.05}),
    ce_period("2026-11-06", {"Amazon Elastic Kubernetes Service": 17.12, "Amazon S3": 0.05,
                             "Amazon Elastic Compute Cloud - Compute": 2.40}),
    ce_period("2026-11-07", {"Amazon Elastic Kubernetes Service": 17.12, "Amazon S3": 0.05,
                             "Amazon Elastic Compute Cloud - Compute": 2.40}),
]


def test_parse_skips_zero_cost_services():
    daily = ca.parse_cost_explorer([ce_period("2026-11-01", {"A": 1.5, "B": 0.0})])
    assert daily == {"2026-11-01": {"A": 1.5}}


def test_summary_totals_and_percentages():
    s = ca.summarize(ca.parse_cost_explorer(EKS_SPIKE))
    assert s["total_cost"] == 40.0
    assert s["highest_spend_day"]["date"] in ("2026-11-06", "2026-11-07")
    assert s["top_services"][0]["service"] == "Amazon Elastic Kubernetes Service"
    assert s["top_services"][0]["pct_of_total"] == 87.1
    assert s["trend"] == "rising"


def test_detects_eks_spike():
    anomalies = ca.detect_anomalies(ca.parse_cost_explorer(EKS_SPIKE))
    eks = [a for a in anomalies if a["service"] == "Amazon Elastic Kubernetes Service"]
    assert eks[0]["date"] == "2026-11-06"
    assert eks[0]["type"] == "spike"
    assert eks[0]["baseline_daily_cost"] == 0.12
    assert eks[0]["cost"] == 17.12
    assert eks[0]["ratio"] == 142.7
    assert len(eks) == 1 and eks[0]["days_elevated"] == 2
    assert eks[0]["projected_monthly_cost"] == 513.6


def test_detects_new_spend_from_forgotten_resource():
    anomalies = ca.detect_anomalies(ca.parse_cost_explorer(EKS_SPIKE))
    ec2 = [a for a in anomalies if a["service"].startswith("Amazon Elastic Compute")]
    assert ec2 and ec2[0]["type"] == "new_spend"


def test_ignores_small_noise():
    noisy = [ce_period(f"2026-11-0{i}", {"Amazon S3": c}) for i, c in enumerate([0.01, 0.01, 0.05, 0.01], 1)]
    assert ca.detect_anomalies(ca.parse_cost_explorer(noisy)) == []


def test_flat_spend_has_no_anomalies():
    flat = [ce_period(f"2026-11-0{i}", {"Amazon RDS": 5.0}) for i in range(1, 8)]
    s = ca.summarize(ca.parse_cost_explorer(flat))
    assert s["trend"] == "flat"
    assert ca.detect_anomalies(ca.parse_cost_explorer(flat)) == []


def test_empty_account():
    s = ca.summarize({})
    assert s["total_cost"] == 0
    assert ca.detect_anomalies({}) == []


# ---------- handler with fake AWS clients ----------
class FakeCE:
    def get_cost_and_usage(self, **kwargs):
        assert kwargs["Granularity"] == "DAILY"
        return {"ResultsByTime": EKS_SPIKE}


class FakeBedrock:
    def __init__(self, fail=False):
        self.fail = fail

    def converse(self, **kwargs):
        if self.fail:
            raise RuntimeError("throttled")
        return {
            "output": {"message": {"content": [{"text": "SUMMARY\nSpend is rising."}]}},
            "usage": {"inputTokens": 500, "outputTokens": 100},
        }


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, **kwargs):
        self.sent.append(kwargs)


def test_handler_end_to_end(monkeypatch):
    import handler

    monkeypatch.setattr(handler, "TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
    sns = FakeSNS()
    result = handler.run(FakeCE(), FakeBedrock(), sns, now=datetime(2026, 11, 8, 8, tzinfo=timezone.utc))
    assert result["total_cost"] == 40.0
    assert result["anomalies"] == 2  # EKS spike + new EC2 spend, each reported once
    assert "ANOMALIES" in sns.sent[0]["Subject"]
    assert len(sns.sent[0]["Subject"]) <= 100
    assert "Spend is rising." in sns.sent[0]["Message"]


def test_email_still_sent_when_bedrock_fails(monkeypatch):
    import handler

    monkeypatch.setattr(handler, "TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
    sns = FakeSNS()
    handler.run(FakeCE(), FakeBedrock(fail=True), sns, now=datetime(2026, 11, 8, tzinfo=timezone.utc))
    assert "AI analysis unavailable" in sns.sent[0]["Message"]
    assert "$17.12" in sns.sent[0]["Message"]