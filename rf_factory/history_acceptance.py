"""Opt-in acceptance strategies for evidence-backed historical operations."""
from __future__ import annotations

import json
import re

from .common import FactoryError


def _patch(content, path="rf_acceptance/test_rf_history.py"):
    lines = content.splitlines(keepends=True)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" +
            "".join("+" + line for line in lines))


class AcceptanceStrategy:
    strategy_id = ""

    def matches(self, record, classification, evidence):
        raise NotImplementedError

    def build(self, record, classification, evidence):
        raise NotImplementedError


class VersionRenderingReuse(AcceptanceStrategy):
    """Keep the previously validated behavior probe as a narrow specialization."""
    strategy_id = "version-rendering-reuse-v1"

    def matches(self, record, classification, evidence):
        symbols = {item.get("symbol") for item in evidence.get("classes_added", [])}
        changed = {item.get("symbol") for item in evidence.get("functions_changed", [])}
        return ("_TrimmedRelease" in symbols and "canonicalize_version" in changed and
                "Version.__str__" in record.get("description", ""))

    def build(self, record, classification, evidence):
        return {"id": self.strategy_id, "goal":
            "Refactor canonicalize_version to share Version's string rendering while preserving its public behavior, including trailing-zero handling and invalid version input.",
            "invariants": ("Preserve canonicalized output and invalid-version fallback.",),
            "compatibility": ("Keep the public function signature and existing regression tests passing.",),
            "requirements": ("Reuse the existing Version string renderer for canonicalization.",),
            "coverage": ["version_renderer_reuse", "string_input_dispatch", "public_behavior"],
            "noop_failure_marker": "test_version_string_rendering_is_reused",
            "test_patch": _patch('''import unittest
from unittest.mock import patch

from packaging.utils import canonicalize_version
from packaging.version import Version


class TestVersionRenderingReuse(unittest.TestCase):
    def test_version_string_rendering_is_reused(self):
        with patch.object(Version, "__str__", return_value="rendered-by-Version"):
            self.assertEqual(canonicalize_version(Version("1.2.3"), strip_trailing_zero=False),
                             "rendered-by-Version")

    def test_string_input_uses_version_rendering(self):
        with patch.object(Version, "__str__", return_value="rendered-by-Version"):
            self.assertEqual(canonicalize_version("1.2.3", strip_trailing_zero=False),
                             "rendered-by-Version")

    def test_public_behavior_is_preserved(self):
        self.assertEqual(canonicalize_version("1.0.0"), "1")
        self.assertEqual(canonicalize_version("1.0.0", strip_trailing_zero=False), "1.0.0")
        self.assertEqual(canonicalize_version("not a version"), "not a version")
''')}


class StaticStructuralOperations(AcceptanceStrategy):
    """Verify extracted or relocated definitions and their production callers."""
    strategy_id = "static-structural-operations-v2"
    supported = {"extraction", "relocation", "interface_evolution"}

    def matches(self, record, classification, evidence):
        operations = classification.get("operations", [])
        return bool(operations) and all(item["kind"] in self.supported and
            (item["kind"] == "interface_evolution" or item.get("callers"))
            for item in operations)

    def build(self, record, classification, evidence):
        operations = classification["operations"]
        def logical_module(path):
            return path.replace("/", ".").removesuffix(".py").removeprefix("src.")
        requirements = []
        for item in operations:
            if item["kind"] == "extraction":
                requirements.append(f"Extract {item['symbol']} and use it from {', '.join(item['callers'])}.")
            elif item["kind"] == "relocation":
                requirements.append(f"Move {item['from_symbol']} from {logical_module(item['from_module'])} to "
                                    f"{logical_module(item['to_module'])} and update its callers.")
            else:
                old, new = item["before"], item["after"]
                changed_defaults = {name: value for name, value in new["defaults"].items()
                                    if old["defaults"].get(name) != value}
                if old["positional"] == new["positional"] and changed_defaults:
                    details = ", ".join(f"{name}={value}" for name, value in changed_defaults.items())
                    requirements.append(f"Allow {item['symbol']} to use the new parameter default {details}.")
                else:
                    requirements.append(f"Evolve {item['symbol']}'s parameter contract to "
                                        f"{new['positional']} with defaults {new['defaults']}.")
        title = re.sub(r"\.py\b", "", record["title"].strip()).rstrip(".")
        goal = f"{title}. " + " ".join(requirements)
        return {"id": self.strategy_id, "goal": goal,
            "invariants": ("Preserve the existing observable behavior covered by the regression suite.",),
            "compatibility": ("Keep unaffected public interfaces working.",),
            "requirements": tuple(requirements),
            "coverage": [item["kind"] for item in operations],
            "noop_failure_marker": "test_all_recorded_operations",
            "test_patch": self._test_patch(operations)}

    @staticmethod
    def _test_patch(operations, path="rf_acceptance/test_rf_history.py",
                    test_name="test_all_recorded_operations"):
        encoded = json.dumps(operations, ensure_ascii=True)
        content = '''import ast
import json
import unittest
from pathlib import Path

ROOT = Path("/testbed")
OPERATIONS = json.loads(''' + repr(encoded) + ''')


def definitions(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found[node.name + "." + member.name] = member
    return found


def calls(node):
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                result.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                result.add(child.func.attr)
    return result


class TestHistoricalStructure(unittest.TestCase):
    def TEST_NAME(self):
        for item in OPERATIONS:
            with self.subTest(item=item):
                kind = item["kind"]
                if kind == "extraction":
                    defs = definitions(item["module"])
                    self.assertIn(item["symbol"], defs)
                    self.assertGreaterEqual(len(ast.unparse(defs[item["symbol"]]).splitlines()), 3)
                    for caller in item["caller_locations"]:
                        self.assertIn(item["symbol"],
                                      calls(definitions(caller["module"])[caller["symbol"]]))
                elif kind == "relocation":
                    self.assertNotIn(item["from_symbol"], definitions(item["from_module"]))
                    defs = definitions(item["to_module"])
                    relocated = {item["from_symbol"], item["to_symbol"]} & defs.keys()
                    self.assertTrue(relocated, "relocated definition is missing")
                    for caller in item["callers"]:
                        self.assertTrue({symbol.split(".")[-1] for symbol in relocated} &
                                        calls(defs[caller]), "caller does not use relocated definition")
                elif kind == "interface_evolution":
                    node = definitions(item["module"])[item["symbol"]]
                    args = node.args
                    names = [a.arg for a in (*args.posonlyargs, *args.args)]
                    self.assertEqual(names, item["after"]["positional"])
                    defaults = {name: ast.unparse(value) for name, value in
                        zip(names[len(names) - len(args.defaults):], args.defaults)}
                    defaults.update({a.arg: ast.unparse(value) for a, value in
                        zip(args.kwonlyargs, args.kw_defaults) if value is not None})
                    self.assertEqual(defaults, item["after"]["defaults"])
'''
        return _patch(content.replace("TEST_NAME", test_name), path)


STRATEGIES = (VersionRenderingReuse(), StaticStructuralOperations())


def choose_strategy(record, classification, evidence):
    for strategy in STRATEGIES:
        if strategy.matches(record, classification, evidence):
            return strategy.build(record, classification, evidence)
    raise FactoryError("No automatic acceptance strategy covers every detected operation")
