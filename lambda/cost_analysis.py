"""Pure-Python cost analysis: no AWS calls, fully unit-testable.

Design principle: **the code does the maths, the LLM writes the story.**
Totals, percentages and anomaly flags are computed deterministically here,
so the numbers in the email are always exact. Bedrock only interprets them
and suggests actions.
"""
from __future__ import annotations

import textwrap
from collections import defaultdict
from statistics import median

# Cost Explorer RECORD_TYPE values that represent metered usage. Everything else
# (Tax, Credit, Refund, upfront/recurring fees, Savings Plan negation...) is a
# billing event, not usage: it posts on fixed dates (often the 1st of the month)
# and would otherwise be flagged as a "new spend" anomaly every month.
USAGE_RECORD_TYPES = {"Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"}

# Belt and braces: services that bill monthly lump sums, never daily usage.
LUMP_SUM_SERVICE_PREFIXES = ("Tax", "AWS Support")


def _record_type(group: dict) -> str:
    # Groups are [SERVICE, RECORD_TYPE] when grouped by both; older callers group by SERVICE only.
    keys = group["Keys"]
    return keys[1] if len(keys) > 1 else "Usage"


def parse_cost_explorer(results_by_time: list[dict]) -> dict[str, dict[str, float]]:
    """Turn Cost Explorer `ResultsByTime` into {date: {service: usage cost}}."""
    daily: dict[str, dict[str, float]] = {}
    for period in results_by_time:
        day = period["TimePeriod"]["Start"]
        services: dict[str, float] = {}
        for group in period.get("Groups", []):
            if _record_type(group) not in USAGE_RECORD_TYPES:
                continue
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                services[service] = services.get(service, 0.0) + amount
        daily[day] = services
    return dict(sorted(daily.items()))


def parse_other_charges(results_by_time: list[dict]) -> dict[str, float]:
    """Non-usage charges and credits over the period, by record type (credits are negative)."""
    totals: dict[str, float] = defaultdict(float)
    for period in results_by_time:
        for group in period.get("Groups", []):
            kind = _record_type(group)
            if kind not in USAGE_RECORD_TYPES:
                totals[kind] += float(group["Metrics"]["UnblendedCost"]["Amount"])
    return {k: round(v, 2) for k, v in sorted(totals.items()) if round(v, 2) != 0}


def summarize(
    daily: dict[str, dict[str, float]],
    top_n: int = 5,
    other_charges: dict[str, float] | None = None,
) -> dict:
    """Spend summary: totals, daily trend, highest day, top services."""
    other_charges = other_charges or {}
    day_totals = {day: round(sum(s.values()), 2) for day, s in daily.items()}
    total = round(sum(day_totals.values()), 2)
    days = len(day_totals) or 1

    by_service: dict[str, float] = defaultdict(float)
    for services in daily.values():
        for name, cost in services.items():
            by_service[name] += cost

    top = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    top_services = [
        {
            "service": name,
            "cost": round(cost, 2),
            "pct_of_total": round(100 * cost / total, 1) if total else 0.0,
        }
        for name, cost in top
    ]

    highest_day = max(day_totals, key=day_totals.get) if day_totals else None
    values = list(day_totals.values())
    half = len(values) // 2
    first_avg = sum(values[:half]) / half if half else 0.0
    second_avg = sum(values[half:]) / (len(values) - half) if values else 0.0
    if first_avg == 0:
        trend = "rising" if second_avg > 0 else "flat"
    else:
        change = (second_avg - first_avg) / first_avg
        trend = "rising" if change > 0.10 else "falling" if change < -0.10 else "flat"

    return {
        "period_start": min(daily) if daily else None,
        "period_end": max(daily) if daily else None,
        "total_cost": total,
        "daily_average": round(total / days, 2),
        "daily_totals": day_totals,
        "highest_spend_day": {"date": highest_day, "cost": day_totals.get(highest_day, 0.0)},
        "trend": trend,
        "top_services": top_services,
        "other_charges": other_charges,
        "net_total": round(total + sum(other_charges.values()), 2),
    }


def detect_anomalies(
    daily: dict[str, dict[str, float]],
    ratio_threshold: float = 3.0,
    min_increase: float = 1.0,
) -> list[dict]:
    """Flag service-days that jump well above that service's earlier baseline.

    A day is anomalous when BOTH are true:
      * cost >= ratio_threshold x baseline (median of the previous days)
      * cost - baseline >= min_increase (in USD), which ignores $0.01 -> $0.05 noise
    A service with no earlier spend that suddenly costs >= min_increase is
    flagged as "new spend", which is how forgotten resources show up.
    """
    days = sorted(daily)
    services = {s for d in daily.values() for s in d if not s.startswith(LUMP_SUM_SERVICE_PREFIXES)}
    anomalies = []

    for service in services:
        series = [daily[d].get(service, 0.0) for d in days]
        found = None
        for i in range(1, len(series)):
            cost = series[i]
            baseline = median(series[:i])
            increase = cost - baseline
            if increase < min_increase:
                continue
            if baseline == 0:
                kind, ratio = "new_spend", None
            elif cost / baseline >= ratio_threshold:
                kind, ratio = "spike", round(cost / baseline, 1)
            else:
                continue
            if found:
                # Same incident still going: count it, don't report it twice.
                found["days_elevated"] += 1
                continue
            found = {
                "date": days[i],
                "service": service,
                "type": kind,
                "baseline_daily_cost": round(baseline, 2),
                "cost": round(cost, 2),
                "increase": round(increase, 2),
                "ratio": ratio,
                "days_elevated": 1,
                "projected_monthly_cost": round(cost * 30, 2),
            }
        if found:
            anomalies.append(found)

    return sorted(anomalies, key=lambda a: a["increase"], reverse=True)


def format_facts(summary: dict, anomalies: list[dict]) -> str:
    """The exact-numbers block at the top of the email (no LLM involved)."""
    lines = [
        f"Period: {summary['period_start']} to {summary['period_end']}",
        (
            f"Total spend: ${summary['total_cost']:.2f}  "
            f"(daily average ${summary['daily_average']:.2f}, trend: {summary['trend']})"
        ),
        (
            f"Highest-spend day: {summary['highest_spend_day']['date']} "
            f"(${summary['highest_spend_day']['cost']:.2f})"
        ),
    ]
    other = summary.get("other_charges") or {}
    if other:
        items = ", ".join(f"{k} {'-' if v < 0 else ''}${abs(v):.2f}" for k, v in other.items())
        lines.append(f"Other charges and credits (not usage): {items}. Net: ${summary['net_total']:.2f}")
    lines += [
        "",
        "Daily totals:",
    ]
    peak = max(summary["daily_totals"].values(), default=0) or 1
    for day, cost in summary["daily_totals"].items():
        bar = "#" * max(1, round(20 * cost / peak)) if cost else ""
        lines.append(f"  {day}  ${cost:>9.2f}  {bar}")

    lines += ["", "Top services:"]
    for s in summary["top_services"]:
        lines.append(f"  {s['service']:<45} ${s['cost']:>9.2f}  {s['pct_of_total']:>5.1f}%")

    lines += ["", f"Anomalies detected: {len(anomalies)}"]
    for a in anomalies[:5]:
        if a["type"] == "spike":
            what = f"${a['baseline_daily_cost']:.2f}/day -> ${a['cost']:.2f} ({a['ratio']}x)"
        else:
            what = f"NEW spend ${a['cost']:.2f}/day"
        lines.append(
            f"  ! {a['date']}  {a['service']}: {what}, elevated {a['days_elevated']} day(s), "
            f"~${a['projected_monthly_cost']:.2f}/month if it continues"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rule-based fallback: used when Bedrock is throttled, over quota or down.
# ---------------------------------------------------------------------------
# (substring of the Cost Explorer service name, advice). First match wins, so
# more specific names come first.
SERVICE_TIPS: list[tuple[str, str]] = [
    ("Elastic Kubernetes", "Delete idle EKS clusters or scale their node groups to 0. Each cluster's "
                           "control plane is billed hourly even with no workloads."),
    ("Elastic Compute Cloud - Compute", "Stop or terminate idle EC2 instances, rightsize with AWS Compute "
                                        "Optimizer, and prefer Graviton instance types."),
    ("EC2 - Other", "Delete unattached EBS volumes and old snapshots, and check NAT gateway data processing."),
    ("Relational Database", "Stop or delete idle RDS instances, turn off Multi-AZ and remove read replicas on "
                            "non-production databases, and delete old manual snapshots."),
    ("Elastic Load Balancing", "Delete load balancers with no healthy targets or traffic. An idle ALB is still "
                               "billed every hour."),
    ("Virtual Private Cloud", "Release unused public IPv4 addresses (billed hourly) and remove unneeded NAT "
                              "gateways or VPC endpoints."),
    ("Simple Storage Service", "Add S3 lifecycle rules (move to Infrequent Access or Glacier), use "
                               "Intelligent-Tiering, and abort incomplete multipart uploads."),
    ("S3", "Add S3 lifecycle rules (move to Infrequent Access or Glacier), use Intelligent-Tiering, and abort "
           "incomplete multipart uploads."),
    ("CloudWatch", "Set retention on log groups, drop unused custom metrics, and avoid high-resolution alarms "
                   "you don't need."),
    ("Lambda", "Rightsize function memory (AWS Lambda Power Tuning) and switch to arm64."),
    ("DynamoDB", "Switch spiky or idle provisioned tables to on-demand, or enable auto scaling."),
    ("ElastiCache", "Rightsize or delete idle cache clusters, and consider reserved nodes for steady use."),
    ("SageMaker", "Stop idle notebook instances and delete unused endpoints, which bill every hour."),
    ("Bedrock", "Use a smaller model or prompt caching, and cap max tokens."),
]
DEFAULT_TIP = "Open Cost Explorer, filter on this service and group by Usage Type to find what is driving the cost."


def tip_for(service: str) -> str:
    for needle, tip in SERVICE_TIPS:
        if needle in service:
            return tip
    return DEFAULT_TIP


def rule_based_recommendations(summary: dict, anomalies: list[dict], n: int = 3) -> list[str]:
    """Top-n actions with no AI: anomalies first (largest increase), then the biggest services."""
    recs: list[str] = []
    seen: set[str] = set()
    for a in anomalies:  # already sorted by increase, largest first
        if len(recs) == n:
            break
        what = "cost spike" if a["type"] == "spike" else "new spend"
        recs.append(
            f"{a['service']} ({what} since {a['date']}, ~${a['projected_monthly_cost']:.2f}/month if it "
            f"continues): {tip_for(a['service'])}"
        )
        seen.add(a["service"])
    for s in summary["top_services"]:
        if len(recs) == n:
            break
        if s["service"] in seen or s["cost"] <= 0:
            continue
        recs.append(f"{s['service']} (${s['cost']:.2f}, {s['pct_of_total']}% of spend): {tip_for(s['service'])}")
        seen.add(s["service"])
    if not recs:
        recs.append("No spend in this period, so there is nothing to optimise.")
    return recs


def format_fallback(summary: dict, anomalies: list[dict], reason: str) -> str:
    """Plain-text replacement for the AI section."""
    lines = [
        f"AI analysis unavailable ({reason}). The figures above are exact.",
        "",
        "TOP 3 RECOMMENDATIONS (rule-based fallback)",
    ]
    for i, rec in enumerate(rule_based_recommendations(summary, anomalies), 1):
        lines.append(textwrap.fill(rec, width=90, initial_indent=f"{i}. ", subsequent_indent="   "))
    return "\n".join(lines)
