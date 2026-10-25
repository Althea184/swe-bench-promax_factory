"""Generate the existing independent synthetic acceptance test and eval script."""
import base64
import shlex


def test_patch_for(spec, path="tests/test_rf_migration.py", accepted_symbols=None):
    """New test checks the requirement independently of the reference patch."""
    content = '''import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_PATH = %r
SYMBOLS = %r
PARAMETER = %r
CALLERS = %r


class TestRefactoringContract(unittest.TestCase):
    def test_public_api_requires_named_argument(self):
        tree = ast.parse((ROOT / API_PATH).read_text(encoding="utf-8"))
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in SYMBOLS]
        self.assertEqual(len(matches), 1)
        fn = matches[0]
        self.assertEqual([arg.arg for arg in fn.args.kwonlyargs], [PARAMETER])
        self.assertEqual(len(fn.args.args), 1)

    def test_static_callers_migrated(self):
        for path, count in CALLERS.items():
            with self.subTest(path=path):
                tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and (getattr(node.func, "id", None) in SYMBOLS or
                              getattr(node.func, "attr", None) in SYMBOLS)]
                self.assertGreaterEqual(len(calls), count)
                self.assertTrue(all(len(node.args) == 1 and len(node.keywords) == 1 and
                                    node.keywords[0].arg == PARAMETER for node in calls))
''' % (spec.entities["api_module"], tuple(accepted_symbols or (spec.entities["target_api"],)),
       spec.entities["keyword_parameter"], spec.metadata["call_counts"])
    lines = content.splitlines(keepends=True)
    patch = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
             f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" +
             "".join("+" + line for line in lines))
    return patch


def eval_script(test_patch, test_argv, test_pattern="test_rf_migration.py",
                test_dir="tests"):
    encoded = base64.b64encode(test_patch.encode()).decode()
    return ("#!/bin/bash\nset -uo pipefail\ncd /testbed\n"
            "export PIP_NO_INDEX=1 UV_OFFLINE=1 PYTHONPATH=/testbed\n"
            f"echo '{encoded}' | base64 -d > /tmp/rf-test.patch\n"
            "if ! git apply /tmp/rf-test.patch; then echo OMNIGRIL_TEST_PATCH_APPLY_FAILED=1; exit 1; fi\n"
            f"{shlex.join(test_argv)} && python -m unittest discover -s {shlex.quote(test_dir)} -p {shlex.quote(test_pattern)}\n"
            "rc=$?\necho OMNIGRIL_EXIT_CODE=$rc\nexit $rc\n")


