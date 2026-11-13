import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

import cost_analysis as ca  # noqa: E402


def ce_period(day, services, record_type="Usage"):
    """One Cost Explorer day, grouped by [SERVICE, RECORD_TYPE] like the Lambda requests."""
    return {
        "TimePeriod": {"Start": day, "End": day},
        "Groups": [
            {"Keys": [name, record_type], "Metrics": {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}}}
            for name, cost in services.items()
        ],
    }


def with_charges(period, services, record_type):
    """Add non-usage rows (Tax, Credit, Fee...) to a Cost Explorer day."""
    period["Groups"] += ce_period(period["TimePeriod"]["Start"], services, record_type)["Groups"]
    return period


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
        assert [g["Key"] for g in kwargs["GroupBy"]] == ["SERVICE", "RECORD_TYPE"]
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
    msg = sns.sent[0]["Message"]
    assert "ANALYSIS (rule-based fallback)" in msg
    assert "AI analysis unavailable (RuntimeError)" in msg
    assert "TOP 3 RECOMMENDATIONS (rule-based fallback)" in msg
    assert "1. Amazon Elastic Kubernetes Service (cost spike since 2026-11-06" in msg
    assert "$17.12" in msg


# ---------- billing events must not look like anomalies ----------
def _month_turn(extra_record_type):
    days = [ce_period(f"2026-10-{d}", {"Amazon RDS": 5.0}) for d in range(26, 32)]
    first = ce_period("2026-11-01", {"Amazon RDS": 5.0})
    return days + [with_charges(first, {"AWS Support (Developer)": 29.0, "Tax": 3.0}, extra_record_type)]


def test_monthly_fees_and_tax_are_not_anomalies():
    raw = _month_turn("Fee")
    assert ca.detect_anomalies(ca.parse_cost_explorer(raw)) == []
    s = ca.summarize(ca.parse_cost_explorer(raw), other_charges=ca.parse_other_charges(raw))
    assert s["total_cost"] == 35.0  # usage only
    assert s["other_charges"] == {"Fee": 32.0}
    assert s["net_total"] == 67.0


def test_lump_sum_services_ignored_even_if_reported_as_usage():
    assert ca.detect_anomalies(ca.parse_cost_explorer(_month_turn("Usage"))) == []


def test_credits_are_reported_not_dropped():
    raw = [with_charges(ce_period("2026-11-01", {"Amazon EC2": 10.0}), {"Amazon EC2": -10.0}, "Credit")]
    s = ca.summarize(ca.parse_cost_explorer(raw), other_charges=ca.parse_other_charges(raw))
    assert s["total_cost"] == 10.0
    assert s["net_total"] == 0.0
    assert "Credit -$10.00" in ca.format_facts(s, [])


# ---------- rule-based fallback ----------
def test_rule_based_recommendations_prioritise_anomalies():
    daily = ca.parse_cost_explorer(EKS_SPIKE)
    recs = ca.rule_based_recommendations(ca.summarize(daily), ca.detect_anomalies(daily))
    assert len(recs) == 3
    assert recs[0].startswith("Amazon Elastic Kubernetes Service (cost spike since 2026-11-06, ~$513.60/month")
    assert recs[1].startswith("Amazon Elastic Compute Cloud - Compute (new spend since 2026-11-06")
    assert recs[2].startswith("Amazon S3 ($0.36, 0.9% of spend)")


def test_rule_based_recommendations_for_real_week():
    # The leftover multi-Region DR project from the README's live results.
    week = [ce_period("2026-11-07", {
        "Amazon Relational Database Service": 0.13,
        "Amazon Elastic Load Balancing": 0.09,
        "Amazon Virtual Private Cloud": 0.04,
        "Amazon Elastic Compute Cloud - Compute": 0.03,
    })]
    recs = ca.rule_based_recommendations(ca.summarize(ca.parse_cost_explorer(week)), [])
    assert [r.split(" (")[0] for r in recs] == [
        "Amazon Relational Database Service",
        "Amazon Elastic Load Balancing",
        "Amazon Virtual Private Cloud",
    ]
    assert "RDS" in recs[0] and "load balancers" in recs[1] and "IPv4" in recs[2]


def test_rule_based_recommendations_empty_account():
    assert ca.rule_based_recommendations(ca.summarize({}), []) == [
        "No spend in this period, so there is nothing to optimise."
    ]


def test_unknown_service_gets_default_tip():
    assert ca.tip_for("Some Brand-New AWS Service") == ca.DEFAULT_TIP
    assert "EKS" in ca.tip_for("Amazon Elastic Kubernetes Service")
