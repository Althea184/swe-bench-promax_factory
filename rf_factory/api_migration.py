"""Conservative, deterministic pilot for a two-argument Python API migration."""
import ast
import difflib
import re
import shutil
from pathlib import Path

from .common import FactoryError, digest, relative
from .models import Candidate, FileChange, GoldPatch, TaskSpec
from .repository import IGNORED


class ApiMigrationDiscovery:
    """Accept only a module-level function and direct imports in two or more modules."""
    old_api = "render_amount"
    new_api = "format_amount"

    def discover(self, repo, config):
        candidates, diagnostics = [], []
        for path in sorted(repo.files):
            if not path.endswith(".py") or path.startswith(config.test_dir + "/"):
                continue
            try:
                tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
            except (UnicodeError, SyntaxError) as exc:
                diagnostics.append({"path": path, "error": str(exc)})
                continue
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == self.old_api]
            if len(functions) != 1:
                continue
            fn = functions[0]
            if (fn.decorator_list or len(fn.args.posonlyargs) or len(fn.args.args) != 2
                    or fn.args.defaults or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg):
                diagnostics.append({"path": path, "error": "unsupported API signature"})
                continue
            module = Path(path).stem
            caller_diagnostics = []
            callers = self._callers(repo, config, module, caller_diagnostics)
            diagnostics.extend(caller_diagnostics)
            if len(callers) < 2:
                diagnostics.append({"path": path, "error": "fewer than two direct-import caller modules"})
                continue
            identity = digest(f"{repo.content_sha256}:{path}:{fn.lineno}:api-migration-v1".encode())[:20]
            candidates.append(Candidate(identity, path, fn.name, fn.lineno, fn.end_lineno, repo.files[path],
                                        f"Direct imports and calls in {len(callers)} modules", "api-migration-v1"))
        return candidates, diagnostics

    def _callers(self, repo, config, module, diagnostics=None):
        result = {}
        for path in sorted(repo.files):
            if not path.endswith(".py") or path.startswith(config.test_dir + "/") or Path(path).stem == module:
                continue
            try:
                tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError) as exc:
                if diagnostics is not None:
                    diagnostics.append({"path": path, "error": str(exc)})
                continue
            imports = [a for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == module
                       for a in n.names if a.name == self.old_api and a.asname is None]
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == self.old_api and len(n.args) == 2 and not n.keywords]
            # Reject references that a direct-name rewrite would leave ambiguous.
            names = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == self.old_api]
            if len(imports) == 1 and calls and len(names) == len(calls):
                result[path] = len(calls)
        return result


class ApiMigrationSpecBuilder:
    def build(self, repo, candidate, config):
        if candidate.strategy != "api-migration-v1" or repo.files.get(candidate.path) != candidate.source_sha256:
            raise FactoryError("Stale or unsupported migration candidate")
        discovery = ApiMigrationDiscovery()
        callers = discovery._callers(repo, config, Path(candidate.path).stem)
        if len(callers) < 2:
            raise FactoryError("Migration needs two caller modules")
        return TaskSpec(candidate, "api_migration",
                        "Use clearer naming for the public amount-formatting API and migrate dependent callers.",
                        (candidate.path, *callers),
                        {"api_module": candidate.path, "current_api": discovery.old_api,
                         "target_api": discovery.new_api},
                        ("Expose the replacement API with keyword-only currency.",
                         "Migrate dependent direct imports and calls.", "Remove the old API definition."),
                        ("Preserve formatted output and validation errors.",
                         "Keep existing regression tests passing."),
                        ("Dependent callers must use the replacement API and supply currency explicitly by name.",),
                        {"strategy": "python-api-migration-v1", "call_counts": callers})


class ApiMigrationGoldGenerator:
    def generate(self, repo, spec):
        api_module = spec.entities["api_module"]
        old_api = spec.entities["current_api"]
        new_api = spec.entities["target_api"]
        call_counts = spec.metadata["call_counts"]
        changes = []
        for path in spec.scope:
            with (repo.snapshot / path).open("r", encoding="utf-8", newline="") as stream:
                source = stream.read()
            tree = ast.parse(source)
            edits = []
            lines = source.splitlines(keepends=True)
            starts = [0]
            for line in lines:
                starts.append(starts[-1] + len(line.encode("utf-8")))

            def pos(lineno, column):
                return starts[lineno - 1] + column

            def replace(node, replacement):
                edits.append((pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset), replacement.encode()))

            if path == api_module:
                targets = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == old_api]
                if len(targets) != 1:
                    raise FactoryError("API definition changed")
                fn = targets[0]
                if fn.decorator_list or len(fn.args.args) != 2 or fn.args.defaults or fn.args.kwonlyargs:
                    raise FactoryError("Unsupported API definition")
                currency = fn.args.args.pop()
                header = lines[fn.lineno - 1].encode("utf-8")
                match = re.search(rb"\bdef\s+" + old_api.encode() + rb"\b", header)
                if match is None:
                    raise FactoryError("Unsupported API definition layout")
                start = starts[fn.lineno - 1] + match.end() - len(old_api)
                edits.append((start, start + len(old_api), new_api.encode()))
                edits.append((pos(currency.lineno, currency.col_offset), pos(currency.lineno, currency.col_offset), b"*, "))
            else:
                imports = 0
                calls = 0
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == Path(api_module).stem:
                        for alias in node.names:
                            if alias.name == old_api and alias.asname is None:
                                replace(alias, new_api)
                                imports += 1
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == old_api:
                        if len(node.args) != 2 or node.keywords:
                            raise FactoryError("Unsupported call shape")
                        replace(node.func, new_api)
                        second = node.args[1]
                        edits.append((pos(second.lineno, second.col_offset), pos(second.lineno, second.col_offset), b"currency="))
                        calls += 1
                if imports != 1 or calls != call_counts[path]:
                    raise FactoryError("Planned call sites changed")
            raw = source.encode("utf-8")
            for start, end, replacement in sorted(edits, reverse=True):
                raw = raw[:start] + replacement + raw[end:]
            after = raw.decode("utf-8")
            ast.parse(after)
            if after == source:
                raise FactoryError("Gold transformation produced no change")
            diff = list(difflib.unified_diff(source.splitlines(keepends=True), after.splitlines(keepends=True),
                                              fromfile=f"a/{path}", tofile=f"b/{path}"))
            added = sum(line.startswith("+") and not line.startswith("+++") for line in diff)
            deleted = sum(line.startswith("-") and not line.startswith("---") for line in diff)
            changes.append(FileChange(path, digest(source.encode()), digest(after.encode()), source, after, added, deleted))
        patch_text = render_patch(changes)
        return GoldPatch(tuple(changes), digest(patch_text.encode()),
                         {"strategy": spec.metadata["strategy"], "old_api": old_api, "new_api": new_api,
                          "call_counts": call_counts})


def render_patch(changes):
    return "".join(line for change in changes for line in difflib.unified_diff(
        change.before.splitlines(keepends=True), change.after.splitlines(keepends=True),
        fromfile=f"a/{change.path}", tofile=f"b/{change.path}"))


class PatchApplier:
    def apply(self, repo, patch, workspace: Path):
        if workspace.exists():
            raise FactoryError("Gold workspace already exists")
        paths = [relative(c.path) for c in patch.changes]
        if len(paths) != len(set(paths)) or len(paths) < 3 or digest(render_patch(patch.changes).encode()) != patch.patch_sha256:
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
