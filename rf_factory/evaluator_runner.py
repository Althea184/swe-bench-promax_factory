"""Portable, standard-library evaluator copied into each pilot artifact."""
import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys


def evaluate(root: Path, spec: dict):
    checks = {}
    details = {}
    api_path = root / spec["api_module"]
    try:
        tree = ast.parse(api_path.read_text(encoding="utf-8"))
        new = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == spec["new_api"]]
        old = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == spec["old_api"]]
        checks["new_api_signature"] = len(new) == 1 and len(new[0].args.args) == 1 and len(new[0].args.kwonlyargs) == 1 and new[0].args.kwonlyargs[0].arg == "currency"
        checks["old_api_removed"] = not old
        checks["api_file_modified"] = spec["base_hashes"][spec["api_module"]] != sha(api_path)
    except (OSError, SyntaxError, UnicodeError) as exc:
        checks.update(new_api_signature=False, old_api_removed=False, api_file_modified=False)
        details["api_error"] = str(exc)
    for path, expected in spec["call_counts"].items():
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            imports = [alias for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == spec["api_module"].removesuffix(".py") for alias in n.names]
            new_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == spec["new_api"] and len(n.args) == 1 and len(n.keywords) == 1 and n.keywords[0].arg == "currency"]
            old_uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == spec["old_api"]]
            checks[f"caller:{path}"] = (sum(a.name == spec["new_api"] and a.asname is None for a in imports) == 1
                                        and not any(a.name == spec["old_api"] for a in imports)
                                        and len(new_calls) == expected and not old_uses
                                        and sha(root / path) != spec["base_hashes"][path])
        except (OSError, SyntaxError, UnicodeError) as exc:
            checks[f"caller:{path}"] = False
            details[f"caller_error:{path}"] = str(exc)
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    runner = Path(__file__).with_name("test_runner.py")
    report = root / ".rf_evaluator_tests.json"
    try:
        process = subprocess.run([sys.executable, "-B", str(runner), spec["test_dir"], spec["test_pattern"], str(report)],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=spec["timeout"])
        counts = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        checks["regression_tests"] = process.returncode == 0 and counts.get("tests_run", 0) > 0 and all(
            counts.get(k) == 0 for k in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"))
        details["regression"] = {"returncode": process.returncode, "counts": counts,
                                 "stdout": process.stdout, "stderr": process.stderr}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        checks["regression_tests"] = False
        details["regression_error"] = str(exc)
    finally:
        report.unlink(missing_ok=True)
    try:
        code = ("import importlib,json,sys;sys.path.insert(0,'.');"
                "fn=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]);"
                "print(json.dumps([fn(value,currency=currency) for value,currency in json.loads(sys.argv[3])]))")
        process = subprocess.run([sys.executable, "-B", "-c", code, spec["api_module"].removesuffix(".py"),
                                  spec["new_api"], json.dumps(spec["functional_inputs"])],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=spec["timeout"])
        checks["functional_api"] = process.returncode == 0 and json.loads(process.stdout) == spec["functional_expected"]
        details["functional"] = {"returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        checks["functional_api"] = False
        details["functional_error"] = str(exc)
    passed = bool(checks) and all(checks.values())
    return {"passed": passed, "score": 1.0 if passed else 0.0, "checks": checks, "details": details}


def sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    result = evaluate(Path(sys.argv[1]).resolve(), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
