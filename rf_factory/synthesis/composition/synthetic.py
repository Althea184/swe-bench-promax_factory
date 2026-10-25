"""Select and compose two related synthetic API migrations."""
import ast
import shutil
from pathlib import Path
from ...core.common import FactoryError
from ...core.config import Config
from ...core.process import command, require
from ...discovery.synthetic.api_migration import ApiMigrationDiscovery
from ...discovery.synthetic.usage_analysis import UsageIndex, module_path
from ...repository import RepositoryPreparer
from ..generators.api_migration import ApiMigrationGoldGenerator, ApiMigrationSpecBuilder


def relation(repo, left, right):
    """Require a direct call or a shared production workflow in one API module."""
    if left.symbol == right.symbol:
        return None, "identical symbol names across candidates are ambiguous for compound ordering"
    edges = []
    paths = {recipe.module: module_path(recipe.module, repo.files) for recipe in (left, right)}
    indexes = {(recipe.module, recipe.symbol): UsageIndex(repo.snapshot, repo.files, recipe.module, recipe.symbol)
               for recipe in (left, right)}
    for caller, callee in ((left, right), (right, left)):
        caller_tree = indexes[(callee.module, callee.symbol)].trees[caller.module]
        functions = [node for node in caller_tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == caller.symbol]
        if len(functions) != 1:
            raise ValueError(f"Missing or ambiguous caller {caller.module}.{caller.symbol}")
        fn = functions[0]
        if caller.module == callee.module:
            direct = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == callee.symbol for node in ast.walk(fn))
        else:
            direct = any(fn.lineno <= node.lineno <= fn.end_lineno for node in
                         indexes[(callee.module, callee.symbol)].call_sites().get(paths[caller.module], []))
        if direct:
            edges.append((callee.symbol, caller.symbol))
    first = set(indexes[(left.module, left.symbol)].call_sites())
    second = set(indexes[(right.module, right.symbol)].call_sites())
    shared = sorted(first & second)
    if left.module != right.module and not edges:
        return None, "different API modules without an explicit call relationship"
    if not edges and not shared:
        return None, "no direct call or shared production caller"
    if not edges and left.module.split(".")[-1] in {"common", "utils", "cli"}:
        return None, "shared generic utility caller does not establish one business responsibility"
    if len(edges) > 1:
        return None, "cyclic API dependency"
    return {"kind": "direct_call" if edges else "shared_workflow",
            "direct_edges": edges, "shared_callers": shared,
            "api_modules": sorted({left.module, right.module})}, None


def candidates_for(repo, recipes, test_argv=None, *, include_candidates=False):
    accepted, rejected = [], []
    pattern = "test*.py"
    if test_argv and "-p" in test_argv:
        pattern = test_argv[test_argv.index("-p") + 1]
    regression_calls = set()
    for test_file in (repo.snapshot / "tests").glob(pattern):
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        for item in tree.body:
            if isinstance(item, ast.ImportFrom) and item.module:
                for alias in item.names:
                    local = alias.asname or alias.name
                    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                           and node.func.id == local and len(node.args) == 2
                           for node in ast.walk(tree)):
                        regression_calls.add((item.module, alias.name))
    for recipe in recipes:
        if (recipe.module, recipe.symbol) in regression_calls:
            rejected.append({"symbol": recipe.symbol,
                             "reason": "selected original regression test calls the positional API directly"})
            continue
        config = Config(repo.snapshot, api_migrations=(recipe,), min_caller_modules=1, min_changed_files=2)
        candidates, diagnostics = ApiMigrationDiscovery().discover(repo, config)
        if not candidates:
            rejected.append({"symbol": recipe.symbol, "reason": diagnostics or "candidate rejected"})
            continue
        try:
            spec = ApiMigrationSpecBuilder().build(repo, candidates[0], config)
            accepted.append((recipe, spec))
        except (FactoryError, ValueError) as exc:
            rejected.append({"symbol": recipe.symbol, "reason": str(exc)})
    pairs = []
    for i, (first, first_spec) in enumerate(accepted):
        for second, second_spec in accepted[i + 1:]:
            found, reason = relation(repo, first, second)
            identity = [first.symbol, second.symbol]
            if reason:
                rejected.append({"pair": identity, "reason": reason})
            else:
                pairs.append((first, second, first_spec, second_spec, found))
    pairs.sort(key=lambda pair: (pair[4]["kind"] != "direct_call",
                                 -len(pair[4]["shared_callers"]), pair[0].module,
                                 pair[0].symbol, pair[1].symbol))
    return (pairs, rejected, accepted) if include_candidates else (pairs, rejected)


def ordered_pair(pair):
    first, second, first_spec, second_spec, found = pair
    if found["direct_edges"] and found["direct_edges"][0] == (second.symbol, first.symbol):
        return ((second, second_spec), (first, first_spec))
    return ((first, first_spec), (second, second_spec))


def apply_steps(repo, clone, pair, attempt):
    """Transform an isolated checkout and diff its final tree against HEAD."""
    workspace = attempt / "workspace"
    shutil.copytree(clone, workspace)
    ordered = ordered_pair(pair)
    records = []
    for step, (recipe, original_spec) in enumerate(ordered):
        current = RepositoryPreparer().prepare(workspace, attempt / f"step-{step}-source")
        config = Config(current.snapshot, api_migrations=(recipe,), min_caller_modules=1, min_changed_files=2)
        candidates, diagnostic = ApiMigrationDiscovery().discover(current, config)
        if not candidates:
            raise FactoryError(f"Step {step} rediscovery failed: {diagnostic}")
        spec = ApiMigrationSpecBuilder().build(current, candidates[0], config)
        before = UsageIndex(current.snapshot, current.files, recipe.module, recipe.symbol).call_sites()
        gold = ApiMigrationGoldGenerator().generate(current, spec)
        for change in gold.changes:
            (workspace / change.path).write_bytes(change.after.encode("utf-8"))
        updated = RepositoryPreparer().prepare(workspace, attempt / f"step-{step}-after")
        after = UsageIndex(updated.snapshot, updated.files, recipe.module, recipe.target_symbol).call_sites()
        if {path: len(nodes) for path, nodes in before.items()} != {path: len(nodes) for path, nodes in after.items()}:
            raise FactoryError(f"Step {step} changed the tracked call graph")
        partial = require(command(["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
                                  cwd=workspace), "intermediate git diff")
        (attempt / f"partial-{step}.patch").write_text(partial + "\n", encoding="utf-8")
        records.append({"candidate_id": original_spec.candidate.candidate_id,
                        "symbol": recipe.symbol, "changed_files": [change.path for change in gold.changes],
                        "callers_before": {path: len(nodes) for path, nodes in before.items()},
                        "callers_after": {path: len(nodes) for path, nodes in after.items()}})
    patch = require(command(["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
                            cwd=workspace), "final git diff") + "\n"
    if not patch.strip():
        raise FactoryError("Compound gold patch is empty")
    return patch, records, ordered


