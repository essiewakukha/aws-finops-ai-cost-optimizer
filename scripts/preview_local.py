"""Preview the email locally, using sample data and without any AWS calls.

    python3 scripts/preview_local.py

It uses the EKS-spike sample from the tests and a placeholder AI section, so
you can see the exact email layout before you deploy anything.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "lambda"), str(root / "tests")]

import handler  # noqa: E402
from test_cost_analysis import FakeBedrock, FakeCE, FakeSNS  # noqa: E402

result = handler.run(FakeCE(), FakeBedrock(), FakeSNS(), now=datetime(2026, 11, 8, 8, tzinfo=timezone.utc))
print("Subject:", result["subject"])
print()
print(result["body"])
