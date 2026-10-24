"""Consolidate equivalent local functions into an existing service module."""
import ast
import builtins

from .api_migration import render_patch
from .common import FactoryError, digest
from .models import Candidate, FileChange, GoldPatch, TaskSpec


def module_path(module):
    return module.replace(".", "/") + ".py"


def local_function(root, path, symbol, class_name="", allowed_globals=()):
    with (root / path).open("r", encoding="utf-8", newline="") as stream:
        source = stream.read()
    tree = ast.parse(source)
    owner = tree.body
    if class_name:
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if len(classes) != 1:
            raise ValueError("Source class missing or ambiguous")
        owner = classes[0].body
    matches = [n for n in owner if isinstance(n, ast.FunctionDef) and n.name == symbol]
    if len(matches) != 1 or matches[0].decorator_list:
        raise ValueError("Source function missing, ambiguous, or decorated")
    fn = matches[0]
    if fn.args.vararg or (fn.args.kwarg and not class_name) or fn.args.kwonlyargs or fn.args.posonlyargs:
        raise ValueError("Unsupported source function signature")
    params = {arg.arg for arg in fn.args.args}
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)
    assigned = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    loaded = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if loaded - params - assigned - set(dir(builtins)) - set(allowed_globals):
        raise ValueError("Function depends on source-module globals")
    return source, tree, fn


class ResponsibilityDiscovery:
    def discover(self, repo, config):
        candidates, diagnostics = [], []
        for recipe in config.responsibility_migrations:
            paths = [module_path(module) for module in recipe.source_modules]
            target = module_path(recipe.target_module)
            try:
                if target not in repo.files or any(path not in repo.files for path in paths):
                    raise ValueError("Configured module missing")
                target_tree = ast.parse((repo.snapshot / target).read_text(encoding="utf-8"))
                if any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == recipe.symbol for n in target_tree.body):
                    raise ValueError("Target symbol already exists")
                allowed = [item.split(" import ", 1)[1].split(" as ")[-1] for item in recipe.target_imports]
                definitions = [local_function(repo.snapshot, path, recipe.symbol, recipe.class_name, allowed) for path in paths]
                shape = ast.dump(definitions[0][2], include_attributes=False)
                if any(ast.dump(fn, include_attributes=False) != shape for _, _, fn in definitions[1:]):
                    raise ValueError("Source implementations are not equivalent")
                counts = {}
                for path, (_, tree, fn) in zip(paths, definitions):
                    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                             and ((isinstance(n.func, ast.Name) and n.func.id == recipe.symbol)
                                  or (recipe.class_name and isinstance(n.func, ast.Attribute) and n.func.attr == recipe.symbol))]
                    if recipe.class_name:
                        calls = [fn]
                    if not calls:
                        raise ValueError("Source module does not call its local implementation")
                    counts[path] = len(calls)
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                diagnostics.append({"path": paths[0], "error": str(exc)})
                continue
            fn = definitions[0][2]
            identity = digest(f"{repo.content_sha256}:{paths}:{target}:{recipe.symbol}:responsibility-v1".encode())[:20]
            metrics = {"caller_modules": len(paths), "potential_files": len(paths) + 1,
                       "behavioral_invariants": len(recipe.behavioral_invariants),
                       "changes_calling_convention": True, "local_call_counts": counts}
            candidates.append(Candidate(identity, paths[0], recipe.symbol, fn.lineno, fn.end_lineno,
                                        repo.files[paths[0]], "Duplicate responsibility in caller modules",
                                        "module-responsibility-v1", metrics))
        return candidates, diagnostics


class ResponsibilitySpecBuilder:
    def build(self, repo, candidate, config):
        recipe = next((r for r in config.responsibility_migrations
                       if module_path(r.source_modules[0]) == candidate.path and r.symbol == candidate.symbol), None)
        if recipe is None or repo.files.get(candidate.path) != candidate.source_sha256:
            raise FactoryError("Stale responsibility candidate")
        sources = tuple(module_path(module) for module in recipe.source_modules)
        target = module_path(recipe.target_module)
        return TaskSpec(candidate, "module_responsibility_migration", recipe.goal, (*sources, target),
                        {"source_modules": sources, "target_module": target, "target_import": recipe.target_module,
                         "symbol": recipe.symbol, "class_name": recipe.class_name,
                         "target_imports": recipe.target_imports},
                        ("Move the shared implementation into the service module.",
                         "Replace duplicated local implementations and redirect their calls."),
                        recipe.behavioral_invariants, (),
                        {"strategy": "module-responsibility-v1", "call_counts": candidate.metrics["local_call_counts"],
                         "dependency_depth": 1, "functional_cases": recipe.functional_cases})


class ResponsibilityGoldGenerator:
    def generate(self, repo, spec):
        symbol = spec.entities["symbol"]
        target = spec.entities["target_module"]
        alias = "_shared_" + symbol
        class_name = spec.entities["class_name"]
        target_imports = spec.entities["target_imports"]
        allowed = [item.split(" import ", 1)[1].split(" as ")[-1] for item in target_imports]
        changes = []
        first_source, _, first_fn = local_function(repo.snapshot, spec.entities["source_modules"][0], symbol, class_name, allowed)
        function_text = "\n".join(first_source.splitlines()[first_fn.lineno - 1:first_fn.end_lineno])
        if class_name:
            import textwrap
            function_text = textwrap.dedent(function_text)
        for path in spec.scope:
            with (repo.snapshot / path).open("r", encoding="utf-8", newline="") as stream:
                before = stream.read()
            if path == target:
                after = before.rstrip("\n") + "\n\n\n" + "\n".join(target_imports) + ("\n\n" if target_imports else "") + function_text + "\n"
            else:
                source, tree, fn = local_function(repo.snapshot, path, symbol, class_name, allowed)
                lines = source.splitlines(keepends=True)
                starts = [0]
                for line in lines:
                    starts.append(starts[-1] + len(line.encode("utf-8")))
                if class_name:
                    args = [arg.arg for arg in fn.args.args]
                    if args != ["self", "stage"] or fn.args.kwarg is None or fn.args.kwarg.arg != "details":
                        raise FactoryError("Unsupported class method signature")
                    wrapper = f"    def {symbol}(self, stage, **details):\n        return {alias}(self, stage, **details)\n"
                    edits = [(starts[fn.lineno - 1], starts[fn.end_lineno], wrapper.encode())]
                else:
                    edits = [(starts[fn.lineno - 1], starts[fn.end_lineno], b"")]
                    for node in ast.walk(tree):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                and node.func.id == symbol and not (fn.lineno <= node.lineno <= fn.end_lineno)):
                            edits.append((starts[node.func.lineno - 1] + node.func.col_offset,
                                          starts[node.func.end_lineno - 1] + node.func.end_col_offset, alias.encode()))
                raw = source.encode("utf-8")
                for start, end, replacement in sorted(edits, reverse=True):
                    raw = raw[:start] + replacement + raw[end:]
                body = raw.decode("utf-8")
                body_tree = ast.parse(body)
                prefix = [n for n in body_tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)]
                prefix += [n for n in body_tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
                insert_at = max((n.end_lineno for n in prefix), default=0)
                body_lines = body.splitlines(keepends=True)
                body_lines.insert(insert_at, f"from {spec.entities['target_import']} import {symbol} as {alias}\n")
                after = "".join(body_lines)
            ast.parse(after)
            if before == after:
                raise FactoryError("Responsibility migration produced no change")
            import difflib
            diff = list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True)))
            changes.append(FileChange(path, digest(before.encode()), digest(after.encode()), before, after,
                                      sum(line.startswith("+") and not line.startswith("+++") for line in diff),
                                      sum(line.startswith("-") and not line.startswith("---") for line in diff)))
        patch = render_patch(changes)
        return GoldPatch(tuple(changes), digest(patch.encode()), {"strategy": spec.metadata["strategy"],
                                                            "symbol": symbol, "target_module": target})
