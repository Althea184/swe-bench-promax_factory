"""Repository-independent AST evidence for historical Python refactorings."""
from __future__ import annotations

import ast
from difflib import SequenceMatcher

from ...core.common import FactoryError
from ...core.process import command, require


def _definitions(tree):
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found[f"{node.name}.{member.name}"] = member
    return found


def _calls(node):
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = child.func
            if isinstance(target, ast.Name):
                result.add(target.id)
            elif isinstance(target, ast.Attribute):
                result.add(target.attr)
    return result


def _signature(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = node.args
    positional = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    defaults = {name: ast.unparse(value) for name, value in
                zip(positional[len(positional) - len(args.defaults):], args.defaults)}
    defaults.update({arg.arg: ast.unparse(value) for arg, value in
                     zip(args.kwonlyargs, args.kw_defaults) if value is not None})
    return {"positional": positional, "keyword_only": [arg.arg for arg in args.kwonlyargs],
            "defaults": defaults, "vararg": args.vararg.arg if args.vararg else None,
            "kwarg": args.kwarg.arg if args.kwarg else None}


def _production(path):
    parts = path.replace("\\", "/").split("/")
    return path.endswith(".py") and not any(part in {"tests", "test", "docs", "examples"}
                                                  for part in parts) and not parts[-1].startswith("test_")


def structural_evidence(clone, base, target, changed):
    """Compare real Git objects; retain every change that limits confidence."""
    evidence = {"changed_paths": changed, "production_paths": [], "ancillary_paths": [],
                "functions_changed": [], "classes_added": [], "public_signatures_changed": [],
                "added_definitions": [], "removed_definitions": [], "changed_definitions": [],
                "atomic_operations": [], "unmatched_removals": [], "affected_modules": []}
    definitions = {}
    for path in changed:
        if not _production(path):
            evidence["ancillary_paths"].append(path)
            continue
        evidence["production_paths"].append(path)
        trees = []
        for revision in (base, target):
            exists = require(command(["git", "ls-tree", "--name-only", revision, "--", path],
                                     cwd=clone), "Historical source tree inspection")
            if not exists.strip():
                trees.append(ast.parse(""))
                continue
            read = command(["git", "show", f"{revision}:{path}"], cwd=clone)
            if read.returncode:
                raise FactoryError(f"Cannot read historical source: {revision}:{path}: {read.stderr.strip()}")
            else:
                try:
                    trees.append(ast.parse(read.stdout, filename=path))
                except SyntaxError as exc:
                    raise FactoryError(f"Cannot parse historical Python source {path}: {exc}") from exc
        before, after = map(_definitions, trees)
        definitions[path] = (before, after)
        for name in sorted(after.keys() - before.keys()):
            node = after[name]
            evidence["added_definitions"].append({"module": path, "symbol": name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "calls": sorted(_calls(node))})
            if isinstance(node, ast.ClassDef):
                evidence["classes_added"].append({"module": path, "symbol": name})
        for name in sorted(before.keys() - after.keys()):
            node = before[name]
            evidence["removed_definitions"].append({"module": path, "symbol": name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function"})
        for name in sorted(before.keys() & after.keys()):
            old, new = before[name], after[name]
            if ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False):
                continue
            evidence["changed_definitions"].append({"module": path, "symbol": name,
                "calls_before": sorted(_calls(old)), "calls_after": sorted(_calls(new)),
                "lines_before": len(ast.unparse(old).splitlines()),
                "lines_after": len(ast.unparse(new).splitlines())})
            if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                    new, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "." not in name:
                    evidence["functions_changed"].append({"module": path, "symbol": name})
                old_sig, new_sig = _signature(old), _signature(new)
                if old_sig != new_sig and not name.startswith("_"):
                    evidence["public_signatures_changed"].append(name)
                    evidence["atomic_operations"].append({"kind": "interface_evolution",
                        "module": path, "symbol": name, "before": old_sig, "after": new_sig})

    added = evidence["added_definitions"]
    removed = evidence["removed_definitions"]
    matched_added = set()
    matched_removed = set()
    for old in removed:
        for new in added:
            if old["module"] == new["module"] or old["kind"] != new["kind"]:
                continue
            if old["symbol"].split(".")[-1].lstrip("_") != new["symbol"].split(".")[-1].lstrip("_"):
                continue
            before = definitions[old["module"]][0][old["symbol"]]
            after = definitions[new["module"]][1][new["symbol"]]
            similarity = SequenceMatcher(None, ast.unparse(before), ast.unparse(after)).ratio()
            if similarity < 0.45:
                continue
            callers = [item["symbol"] for item in evidence["changed_definitions"]
                       if item["module"] == new["module"] and
                       new["symbol"].split(".")[-1] in item["calls_after"]]
            evidence["atomic_operations"].append({"kind": "relocation",
                "from_module": old["module"], "to_module": new["module"],
                "from_symbol": old["symbol"], "to_symbol": new["symbol"],
                "similarity": round(similarity, 3), "callers": callers})
            matched_added.add((new["module"], new["symbol"]))
            matched_removed.add((old["module"], old["symbol"]))
            break
    for new in added:
        if (new["module"], new["symbol"]) in matched_added or "." in new["symbol"]:
            continue
        caller_locations = [{"module": item["module"], "symbol": item["symbol"]}
                            for item in evidence["changed_definitions"]
                            if new["symbol"] in item["calls_after"]
                            and new["symbol"] not in item["calls_before"]]
        if caller_locations:
            evidence["atomic_operations"].append({"kind": "extraction", "module": new["module"],
                "symbol": new["symbol"], "component": new["kind"],
                "callers": [item["symbol"] for item in caller_locations],
                "caller_locations": caller_locations})
    evidence["unmatched_removals"] = [item for item in removed if
        (item["module"], item["symbol"]) not in matched_removed and "." not in item["symbol"]]
    evidence["affected_modules"] = sorted(evidence["production_paths"])
    return evidence


def classify(record, evidence):
    """Label supported structural operations, then reject uncertain scopes."""
    import re
    title = record["title"].lower()
    description = record.get("description", "").lower()
    operations = evidence.get("atomic_operations", [])
    kinds = {item["kind"] for item in operations}
    types = []
    if "extraction" in kinds:
        types.append("extraction")
        if len(evidence.get("changed_definitions", [])) > 0:
            types.append("decomposition")
    if "relocation" in kinds:
        types.append("relocation")
    if "interface_evolution" in kinds:
        types.append("interface_evolution")
    symbols = []
    for item in operations:
        symbols.extend(value for value in (item.get("symbol"), item.get("from_symbol"),
                                          item.get("to_symbol")) if value)
        symbols.extend(item.get("callers", ()))
    result = {"types": tuple(types), "operations": operations,
            "symbols": tuple(dict.fromkeys(symbols)),
            "workflow": record["title"].strip(),
              "review_status": "high_confidence"}
    if re.search(r"\b(?:fix|bug|feature|support|security)\b", title) and "refactor" not in title:
        return result, "PR title indicates a behavior change rather than refactoring"
    if re.search(r"\b(?:fixes|closes|resolves)\s+#\d+", description):
        result["review_status"] = "mixed_change"
        return result, "PR explicitly combines refactoring with an issue fix; manual separation required"
    if evidence.get("ancillary_paths"):
        result["review_status"] = "mixed_change"
        return result, "mixed non-production changes cannot be separated conservatively"
    if not operations:
        return result, "no supported structural extraction, relocation, or interface evolution detected"
    if evidence.get("unmatched_removals"):
        result["review_status"] = "needs_review"
        return result, "removed definitions are not explained by detected operations"
    if not evidence.get("production_paths"):
        return result, "no production Python changes"
    return result, None
