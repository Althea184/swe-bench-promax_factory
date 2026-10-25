"""Build API migration task specs and existing Gold patches."""
import ast
import difflib
import re
import shutil
from pathlib import Path
from ...core.common import FactoryError, digest, relative
from ...core.models import FileChange, GoldPatch, TaskSpec
from ...repository import IGNORED
from ...discovery.synthetic.usage_analysis import UsageIndex, module_name


def recipe_for(config, candidate, files):
    return next((item for item in config.api_migrations
                 if module_name(candidate.path, files) == item.module
                 and item.symbol == candidate.symbol), None)


class ApiMigrationSpecBuilder:
    def build(self, repo, candidate, config):
        recipe = recipe_for(config, candidate, repo.files)
        if candidate.strategy != "api-migration-v2" or repo.files.get(candidate.path) != candidate.source_sha256 or recipe is None:
            raise FactoryError("Stale or unsupported migration candidate")
        index = UsageIndex(repo.snapshot, repo.files, recipe.module, recipe.symbol)
        calls = index.call_sites(include_local=config.include_local_calls)
        if len(calls) < config.min_caller_modules:
            raise FactoryError("Migration has too few caller modules")
        structure = (f"Make {recipe.parameter} keyword-only on the public API.",
                     "Migrate all production calls to the new calling convention.")
        if recipe.symbol != recipe.target_symbol:
            structure += ("Replace the old public API name across imports and calls.",)
        return TaskSpec(candidate, "api_migration", recipe.goal,
                        tuple(dict.fromkeys((candidate.path, *sorted(calls)))),
                        {"api_module": candidate.path, "module": recipe.module,
                         "current_api": recipe.symbol, "target_api": recipe.target_symbol,
                         "keyword_parameter": recipe.parameter},
                        structure, recipe.behavioral_invariants, recipe.compatibility_requirements,
                        {"strategy": "python-api-migration-v2", "include_local_calls": config.include_local_calls,
                         "call_counts": {path: len(nodes) for path, nodes in calls.items()},
                         "functional_cases": recipe.functional_cases, "dependency_depth": index.dependency_depth()})


class ApiMigrationGoldGenerator:
    def generate(self, repo, spec):
        api_path = spec.entities["api_module"]
        module = spec.entities["module"]
        old = spec.entities["current_api"]
        new = spec.entities["target_api"]
        parameter = spec.entities["keyword_parameter"]
        index = UsageIndex(repo.snapshot, repo.files, module, old)
        sites = index.call_sites(include_local=spec.metadata.get("include_local_calls", False))
        if set(sites) - {api_path} != set(spec.scope) - {api_path}:
            raise FactoryError("Call graph changed after specification")
        changes = []
        paths = list(spec.scope)
        if old != new:
            for candidate_path in repo.files:
                if candidate_path.endswith(".py") and candidate_path not in paths:
                    candidate_module = module_name(candidate_path, repo.files)
                    tree = index.trees.get(candidate_module)
                    if tree and any(isinstance(node, ast.ImportFrom) and index.import_module(candidate_module, node) == module
                                    and any(alias.name == old for alias in node.names) for node in tree.body):
                        paths.append(candidate_path)
        for path in paths:
            with (repo.snapshot / path).open("r", encoding="utf-8", newline="") as stream:
                source = stream.read()
            tree = ast.parse(source)
            lines = source.splitlines(keepends=True)
            starts = [0]
            for line in lines:
                starts.append(starts[-1] + len(line.encode("utf-8")))

            def pos(line, column):
                return starts[line - 1] + column

            edits = []

            def replace(node, value):
                edits.append((pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset), value.encode()))

            if path == api_path:
                fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == old)
                argument = fn.args.args[1]
                edits.append((pos(argument.lineno, argument.col_offset), pos(argument.lineno, argument.col_offset), b"*, "))
                # UsageIndex excludes the defining module. Direct calls there still
                # need migration (for example a helper calling another public API).
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == old):
                        if len(node.args) != 2 or node.keywords:
                            raise FactoryError("Unsupported same-module call shape")
                        second = node.args[1]
                        edits.append((pos(second.lineno, second.col_offset),
                                      pos(second.lineno, second.col_offset), (parameter + "=").encode()))
                        if old != new:
                            replace(node.func, new)
                if old != new:
                    header = lines[fn.lineno - 1].encode("utf-8")
                    match = re.search(rb"\bdef\s+" + old.encode() + rb"\b", header)
                    if match is None:
                        raise FactoryError("Unsupported API definition layout")
                    start = starts[fn.lineno - 1] + match.end() - len(old)
                    edits.append((start, start + len(old), new.encode()))
            else:
                for call in sites.get(path, []):
                    second = call.args[1]
                    edits.append((pos(second.lineno, second.col_offset), pos(second.lineno, second.col_offset), (parameter + "=").encode()))
                    if old != new:
                        if isinstance(call.func, ast.Name):
                            replace(call.func, new)
                        elif isinstance(call.func, ast.Attribute):
                            replace(call.func, ast.get_source_segment(source, call.func).rsplit(".", 1)[0] + "." + new)
                if old != new:
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and index.import_module(module_name(path, repo.files), node) == module:
                            for alias in node.names:
                                if alias.name == old and alias.asname is None:
                                    replace(alias, new)
            raw = source.encode("utf-8")
            for start, end, replacement in sorted(edits, reverse=True):
                raw = raw[:start] + replacement + raw[end:]
            after = raw.decode("utf-8")
            ast.parse(after)
            if after == source:
                continue
            diff = list(difflib.unified_diff(source.splitlines(keepends=True), after.splitlines(keepends=True),
                                              fromfile=f"a/{path}", tofile=f"b/{path}"))
            added = sum(line.startswith("+") and not line.startswith("+++") for line in diff)
            deleted = sum(line.startswith("-") and not line.startswith("---") for line in diff)
            changes.append(FileChange(path, digest(source.encode()), digest(after.encode()), source, after, added, deleted))
        patch_text = render_patch(changes)
        return GoldPatch(tuple(changes), digest(patch_text.encode()),
                         {"strategy": spec.metadata["strategy"], "old_api": old, "new_api": new,
                          "keyword_parameter": parameter, "call_counts": spec.metadata["call_counts"]})


def render_patch(changes):
    return "".join(
        f"diff --git a/{change.path} b/{change.path}\n" + "".join(difflib.unified_diff(
            change.before.splitlines(keepends=True), change.after.splitlines(keepends=True),
            fromfile=f"a/{change.path}", tofile=f"b/{change.path}"))
        for change in changes)


class PatchApplier:
    def apply(self, repo, patch, workspace: Path):
        if workspace.exists():
            raise FactoryError("Gold workspace already exists")
        paths = [relative(change.path) for change in patch.changes]
        if len(paths) != len(set(paths)) or not paths or digest(render_patch(patch.changes).encode()) != patch.patch_sha256:
            raise FactoryError("Invalid gold patch manifest")
        for change in patch.changes:
            source = repo.snapshot / change.path
            if not source.is_file() or digest(source.read_bytes()) != change.before_sha256 or digest(change.before.encode()) != change.before_sha256:
                raise FactoryError(f"Gold preimage mismatch: {change.path}")
            if digest(change.after.encode()) != change.after_sha256:
                raise FactoryError(f"Gold postimage mismatch: {change.path}")
        shutil.copytree(repo.snapshot, workspace, ignore=lambda _, names: set(names) & IGNORED)
        try:
            for change in patch.changes:
                (workspace / change.path).write_text(change.after, encoding="utf-8", newline="\n")
                if digest((workspace / change.path).read_bytes()) != change.after_sha256:
                    raise FactoryError(f"Gold write mismatch: {change.path}")
        except Exception:
            shutil.rmtree(workspace)
            raise
        return workspace
