"""Standalone unittest reporter executed in a disposable repository copy."""
import json
from pathlib import Path
import sys
import unittest


def main():
    test_dir, pattern, report = sys.argv[1:]
    sys.path.insert(0, str(Path.cwd()))
    suite = unittest.defaultTestLoader.discover(test_dir, pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    data = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}
    Path(report).write_text(json.dumps(data), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
