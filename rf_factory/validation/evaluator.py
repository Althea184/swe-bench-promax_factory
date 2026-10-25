"""Turn verifier observations into acceptance decisions and diagnostics."""
from ..core.models import Evaluation, PrecheckResult


class Evaluator:
    def evaluate(self, report):
        checks = report["checks"]
        passed = bool(checks) and all(checks.values())
        return Evaluation(passed, sum(checks.values()) / len(checks) if checks else 0.0,
                          checks, report["details"])

    def precheck(self, gold, noop):
        reasons = []
        if not gold.passed:
            reasons.append("gold verification failed")
        if noop.passed:
            reasons.append("unchanged repository passed")
        independent_failures = sum(not value for name, value in noop.checks.items()
                                   if name == "new_api_signature" or name.startswith("caller:") or name == "functional_api")
        if independent_failures < 2:
            reasons.append("no-op distinction is too weak")
        return PrecheckResult(gold, noop, not reasons, tuple(reasons))
