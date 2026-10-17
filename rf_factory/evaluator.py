"""Turn verifier observations into acceptance decisions and diagnostics."""
from .models import Evaluation, PrecheckResult


class Evaluator:
    def evaluate(self, report):
        checks = report["checks"]
        passed = bool(checks) and all(checks.values())
        return Evaluation(passed, sum(checks.values()) / len(checks) if checks else 0.0,
                          checks, report["details"])

    def precheck(self, gold, noop):
        return PrecheckResult(gold, noop, gold.passed and not noop.passed)
