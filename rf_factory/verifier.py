"""Portable structural, regression, and behavioral checks for API migrations."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usage_analysis import UsageIndex  # copied beside this file for rollout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api_structure(root, spec):
    checks, details = {}, {}
    api_path = root / spec["api_module"]
    try:
        tree = ast.parse(api_path.read_text(encoding="utf-8"))
        new = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == spec["new_api"]]
        old = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == spec["old_api"]]
        parameter = spec["keyword_parameter"]
        checks["new_api_signature"] = (len(new) == 1 and len(new[0].args.args) == 1
                                       and len(new[0].args.kwonlyargs) == 1
                                       and new[0].args.kwonlyargs[0].arg == parameter)
        if spec["new_api"] != spec["old_api"]:
            checks["old_api_removed"] = not old
        checks["api_file_modified"] = spec["base_hashes"][spec["api_module"]] != sha(api_path)
    except (OSError, SyntaxError, UnicodeError) as exc:
        checks["new_api_signature"] = checks["api_file_modified"] = False
        if spec["new_api"] != spec["old_api"]:
            checks["old_api_removed"] = False
        details["api_error"] = str(exc)
    try:
        files = [path.relative_to(root).as_posix() for path in root.rglob("*.py")]
        current = UsageIndex(root, files, spec["module"], spec["new_api"])
        current_calls = current.call_sites()
        old_calls = {}
        if spec["new_api"] != spec["old_api"]:
            old_calls = UsageIndex(root, files, spec["module"], spec["old_api"]).call_sites()
        for path in spec["call_counts"]:
            calls = current_calls.get(path, [])
            checks[f"caller:{path}"] = (sha(root / path) != spec["base_hashes"][path]
                                        and all(len(call.args) == 1 and len(call.keywords) == 1
                                                and call.keywords[0].arg == spec["keyword_parameter"] for call in calls)
                                        and not old_calls.get(path))
        checks["all_target_calls_migrated"] = all(
            len(call.args) == 1 and len(call.keywords) == 1 and call.keywords[0].arg == parameter
            for calls in current_calls.values() for call in calls)
    except (OSError, SyntaxError, UnicodeError, KeyError) as exc:
        for path in spec["call_counts"]:
            checks[f"caller:{path}"] = False
        checks["all_target_calls_migrated"] = False
        details["call_graph_error"] = str(exc)
    return checks, details


def responsibility_structure(root, spec):
    checks, details = {}, {}
    symbol = spec["symbol"]
    target = root / spec["target_module"]
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"))
        checks["service_owns_function"] = sum(isinstance(n, ast.FunctionDef) and n.name == symbol for n in tree.body) == 1
        checks["service_modified"] = sha(target) != spec["base_hashes"][spec["target_module"]]
    except (OSError, SyntaxError, UnicodeError) as exc:
        checks["service_owns_function"] = checks["service_modified"] = False
        details["service_error"] = str(exc)
    for path in spec["source_modules"]:
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                       and n.module == spec["target_import"]
                       and any(a.name == symbol for a in n.names)]
            if spec.get("class_name"):
                classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == spec["class_name"]]
                methods = [n for cls in classes for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == symbol]
                calls = [n for method in methods for n in ast.walk(method) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)
                         and any(n.func.id == a.asname or n.func.id == a.name for imp in imports for a in imp.names if a.name == symbol)]
                migrated = len(methods) == 1 and len(methods[0].body) == 1 and len(calls) == 1
            else:
                local = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == symbol]
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                         and any(n.func.id == a.asname or n.func.id == a.name for imp in imports for a in imp.names if a.name == symbol)]
                migrated = not local and len(calls) >= spec["call_counts"][path]
            checks[f"caller:{path}"] = (migrated and bool(imports)
                                         and sha(root / path) != spec["base_hashes"][path])
        except (OSError, SyntaxError, UnicodeError) as exc:
            checks[f"caller:{path}"] = False
            details[f"caller_error:{path}"] = str(exc)
    return checks, details


def verify(root: Path, spec: dict):
    root = Path(root)
    checks, details = {}, {}
    if spec.get("strategy") == "module-responsibility-v1":
        checks, details = responsibility_structure(root, spec)
    else:
        checks, details = api_structure(root, spec)
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    runner = Path(__file__).with_name("test_runner.py")
    report = root / ".rf_evaluator_tests.json"
    try:
        process = subprocess.run([sys.executable, "-B", str(runner), spec["test_dir"], spec["test_pattern"], str(report)],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=spec["timeout"])
        counts = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        checks["regression_tests"] = process.returncode == 0 and counts.get("tests_run", 0) > 0 and all(
            counts.get(key) == 0 for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"))
        details["regression"] = {"returncode": process.returncode, "counts": counts,
                                 "stdout": process.stdout, "stderr": process.stderr}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        checks["regression_tests"] = False
        details["regression_error"] = str(exc)
    finally:
        report.unlink(missing_ok=True)
    code = '''import importlib, json, sys
fn = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])
results = []
for case in json.loads(sys.argv[3]):
    try:
        results.append({"value": fn(*case["args"], **case.get("kwargs", {}))})
    except Exception as exc:
        results.append({"raises": type(exc).__name__, "message": str(exc)})
print(json.dumps(results))
'''
    try:
        process = subprocess.run([sys.executable, "-B", "-c", code, spec["module"], spec["new_api"],
                                  json.dumps(spec["functional_cases"])], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=spec["timeout"])
        actual = json.loads(process.stdout) if process.returncode == 0 else []
        functional = []
        for case, result in zip(spec["functional_cases"], actual):
            if "expected" in case:
                functional.append(result.get("value") == case["expected"] and "raises" not in result)
            else:
                functional.append(result.get("raises") == case["raises"]
                                  and case.get("message", "") in result.get("message", ""))
        checks["functional_api"] = process.returncode == 0 and len(functional) == len(spec["functional_cases"]) and all(functional)
        details["functional"] = {"returncode": process.returncode, "case_passed": functional,
                                  "stdout": process.stdout, "stderr": process.stderr}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        checks["functional_api"] = False
        details["functional_error"] = str(exc)
    if spec.get("class_name"):
        probe = '''import importlib, json, sys, tempfile
from pathlib import Path
results = []
for name in json.loads(sys.argv[1]):
    cls = getattr(importlib.import_module(name), sys.argv[2])
    with tempfile.TemporaryDirectory() as folder:
        obj = cls.__new__(cls)
        obj.output, obj.events, obj.stage = Path(folder), [], ""
        getattr(obj, sys.argv[3])("probe", value=3)
        recorded = json.loads((obj.output / "stages.json").read_text(encoding="utf-8"))
        results.append(obj.stage == "probe" and obj.events == [{"stage": "probe", "value": 3}] and recorded == obj.events)
print(json.dumps(results))
'''
        try:
            modules = [path.removesuffix(".py").replace("/", ".") for path in spec["source_modules"]]
            process = subprocess.run([sys.executable, "-B", "-c", probe, json.dumps(modules),
                                      spec["class_name"], spec["symbol"]], cwd=root, env=env,
                                     capture_output=True, text=True, timeout=spec["timeout"])
            observed = json.loads(process.stdout) if process.returncode == 0 else []
            checks["pipeline_event_behavior"] = len(observed) == len(modules) and all(observed)
            if not spec["functional_cases"]:
                checks["functional_api"] = checks["pipeline_event_behavior"]
            details["pipeline_event"] = {"returncode": process.returncode, "observed": observed,
                                         "stderr": process.stderr}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            checks["pipeline_event_behavior"] = False
            if not spec["functional_cases"]:
                checks["functional_api"] = False
            details["pipeline_event_error"] = str(exc)
    return {"checks": checks, "details": details}


if __name__ == "__main__":
    result = verify(Path(sys.argv[1]).resolve(), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["checks"] and all(result["checks"].values()) else 1)
