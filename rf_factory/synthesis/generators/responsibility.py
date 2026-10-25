"""Build responsibility migration specs and existing Gold patches."""
import ast
from ...core.common import FactoryError, digest
from ...core.models import FileChange, GoldPatch, TaskSpec
from ...discovery.synthetic.responsibility import module_path, local_function
from .api_migration import render_patch


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
