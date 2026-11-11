"""Pure-Python cost analysis: no AWS calls, fully unit-testable.

Design principle: **the code does the maths, the LLM writes the story.**
Totals, percentages and anomaly flags are computed deterministically here,
so the numbers in the email are always exact. Bedrock only interprets them
and suggests actions.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median


def parse_cost_explorer(results_by_time: list[dict]) -> dict[str, dict[str, float]]:
    """Turn Cost Explorer `ResultsByTime` into {date: {service: cost}}."""
    daily: dict[str, dict[str, float]] = {}
    for period in results_by_time:
        day = period["TimePeriod"]["Start"]
        services: dict[str, float] = {}
        for group in period.get("Groups", []):
            service = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                services[service] = services.get(service, 0.0) + amount
        daily[day] = services
    return dict(sorted(daily.items()))


def summarize(daily: dict[str, dict[str, float]], top_n: int = 5) -> dict:
    """Spend summary: totals, daily trend, highest day, top services."""
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
    services = {s for d in daily.values() for s in d}
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