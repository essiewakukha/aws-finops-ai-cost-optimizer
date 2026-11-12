"""AI-Driven FinOps Cost Optimizer: daily Lambda entry point.

Flow: Cost Explorer (last N days, by service) -> deterministic analysis
      -> Amazon Bedrock (Converse API) narrative + recommendations -> SNS email.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

from botocore.config import Config
from cost_analysis import detect_anomalies, format_facts, parse_cost_explorer, summarize

log = logging.getLogger()
log.setLevel(logging.INFO)

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
RATIO_THRESHOLD = float(os.environ.get("ANOMALY_RATIO", "3.0"))
MIN_INCREASE = float(os.environ.get("ANOMALY_MIN_USD", "1.0"))

SYSTEM_PROMPT = """You are a FinOps analyst writing a daily AWS cost email.
You receive pre-computed, EXACT cost facts as JSON. Never recalculate or invent numbers;
only use numbers that appear in the JSON.

Output rules:
- Plain text only. No markdown, no asterisks, no '#' headings, no tables.
- Use exactly these four sections, with the titles in capitals:

SUMMARY
2-3 sentences on total spend, the trend and the highest-spend day.

TOP SERVICES
One line per service: name, cost, percent of total, and a short comment.

ANOMALIES
For each anomaly: what changed, the likely cause for that AWS service, and how to verify it.
If there are none, write: No anomalies detected.

TOP 3 RECOMMENDATIONS
Numbered 1-3. Specific, actionable steps for THESE services (for example: delete idle EKS
clusters/node groups, rightsize, Savings Plans, S3 lifecycle rules, stop idle instances,
remove unattached EBS volumes or Elastic IPs). Include the estimated monthly saving when it
can be derived from the facts.

Keep the whole report under 350 words."""


def _date_range(now: datetime, days: int) -> tuple[str, str]:
    # Cost Explorer's End date is exclusive, so this covers the last N *complete* days.
    end = now.date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def get_costs(ce, start: str, end: str) -> list[dict]:
    results, token = [], None
    while True:
        kwargs = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp["ResultsByTime"])
        token = resp.get("NextPageToken")
        if not token:
            return results


def ai_report(bedrock, facts: dict) -> str:
    # The Converse API gives ONE request/response shape for every Bedrock model,
    # so switching models only means changing BEDROCK_MODEL_ID.
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": json.dumps(facts, indent=2)}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.2},
    )
    usage = resp.get("usage", {})
    log.info("Bedrock tokens: in=%s out=%s", usage.get("inputTokens"), usage.get("outputTokens"))
    return "".join(block.get("text", "") for block in resp["output"]["message"]["content"]).strip()


def run(ce, bedrock, sns, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, end = _date_range(now, LOOKBACK_DAYS)

    daily = parse_cost_explorer(get_costs(ce, start, end))
    summary = summarize(daily)
    anomalies = detect_anomalies(daily, RATIO_THRESHOLD, MIN_INCREASE)
    facts = {"summary": summary, "anomalies": anomalies}
    log.info("Facts: %s", json.dumps(facts))

    try:
        analysis = ai_report(bedrock, facts)
    except Exception as exc:  # the email must still go out
        log.exception("Bedrock call failed")
        analysis = f"(AI analysis unavailable: {exc.__class__.__name__}. The figures above are still exact.)"

    body = "\n".join(
        [
            "AWS DAILY COST REPORT",
            "=" * 60,
            format_facts(summary, anomalies),
            "",
            "=" * 60,
            "AI ANALYSIS (Amazon Bedrock)",
            "=" * 60,
            analysis,
            "",
            "--",
            f"Generated {now:%Y-%m-%d %H:%M} UTC by the FinOps Cost Optimizer. Model: {MODEL_ID}",
        ]
    )

    flag = f" | {len(anomalies)} ANOMAL{'Y' if len(anomalies) == 1 else 'IES'}" if anomalies else ""
    subject = f"AWS cost {start}..{summary['period_end']}: ${summary['total_cost']:.2f}{flag}"[:100]

    if TOPIC_ARN:
        sns.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=body)
    log.info("Subject: %s", subject)
    return {"subject": subject, "anomalies": len(anomalies), "total_cost": summary["total_cost"], "body": body}


def lambda_handler(event, context):
    result = run(
        ce=boto3.client("ce", region_name="us-east-1"),  # Cost Explorer's endpoint is in us-east-1
                # Retry ThrottlingException with exponential back-off (low quotas on new accounts)
        bedrock=boto3.client(
            "bedrock-runtime",
            config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=45),
        ),
        sns=boto3.client("sns"),
    )
    result.pop("body")
    return result