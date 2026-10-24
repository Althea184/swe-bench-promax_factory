"""Two related API migrations in one Docker-gated real instance."""
from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import shlex
import shutil
from dataclasses import asdict
from pathlib import Path

from .api_migration import ApiMigrationDiscovery, ApiMigrationGoldGenerator, ApiMigrationSpecBuilder, render_patch
from .common import FactoryError, digest, write_json
from .config import Config
from .models import CompoundTaskSpec
from .problem_generation import ProblemGenerator
from .repository import RepositoryPreparer
from .usage_analysis import UsageIndex


def relation(repo, left, right):
    """Require a direct call or a shared production workflow in one API module."""
    if left.module != right.module:
        return None, "different API modules without an explicit call relationship"
    tree = ast.parse((repo.snapshot / (left.module.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    edges = []
    for caller, callee in ((left, right), (right, left)):
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == callee.symbol for node in ast.walk(functions[caller.symbol])):
            edges.append((callee.symbol, caller.symbol))
    first = set(UsageIndex(repo.snapshot, repo.files, left.module, left.symbol).call_sites())
    second = set(UsageIndex(repo.snapshot, repo.files, right.module, right.symbol).call_sites())
    shared = sorted(first & second)
    if not edges and not shared:
        return None, "no direct call or shared production caller"
    if not edges and left.module.split(".")[-1] in {"common", "utils", "cli"}:
        return None, "shared generic utility caller does not establish one business responsibility"
    if len(edges) > 1:
        return None, "cyclic API dependency"
    return {"kind": "direct_call" if edges else "shared_workflow",
            "direct_edges": edges, "shared_callers": shared,
            "api_module": left.module}, None


def candidates_for(repo, recipes, test_argv=None):
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
    return pairs, rejected


def ordered_pair(pair):
    first, second, first_spec, second_spec, found = pair
    if found["direct_edges"] and found["direct_edges"][0] == (second.symbol, first.symbol):
        return ((second, second_spec), (first, first_spec))
    return ((first, first_spec), (second, second_spec))


def apply_steps(repo, clone, pair, attempt):
    """Transform an isolated checkout and diff its final tree against HEAD."""
    from .real_pipeline import command, require
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


def synthesize_compound(repo, recipes, output, clone, repo_url, base_commit,
                        image_name, image_id, test_argv, run_script):
    from .real_pipeline import command, eval_script, require, test_patch_for
    pairs, rejected = candidates_for(repo, recipes, test_argv)
    write_json(output / "relations.json", [{"pair": [p[0].symbol, p[1].symbol], **p[4]} for p in pairs])
    write_json(output / "rejections.json", rejected)
    if not pairs:
        raise FactoryError("No related API migration pair found")
    for index, pair in enumerate(pairs):
        attempt = output / "attempts" / str(index)
        attempt.mkdir(parents=True)
        try:
            gold_patch, steps, ordered = apply_steps(repo, clone, pair, attempt)
            specs = tuple(spec for _, spec in ordered)
            edge_names = pair[4]["direct_edges"]
            edge_ids = tuple((next(spec.candidate.candidate_id for spec in specs if spec.candidate.symbol == a),
                              next(spec.candidate.candidate_id for spec in specs if spec.candidate.symbol == b))
                             for a, b in edge_names)
            goal = (f"Migrate the related {specs[0].entities['current_api']} and "
                    f"{specs[1].entities['current_api']} APIs in the {pair[0].module} workflow "
                    "to require named arguments across their production callers.")
            composition = CompoundTaskSpec(specs, edge_ids, goal,
                                           tuple(sorted(set().union(*(set(spec.scope) for spec in specs)))),
                                           {"relation": pair[4], "ordered_symbols": [r.symbol for r, _ in ordered]})
            test_patch = "".join(test_patch_for(spec, f"tests/test_rf_migration_{i}.py")
                                 for i, spec in enumerate(specs))
            script = eval_script(test_patch, test_argv, "test_rf_migration_*.py")
            (attempt / "gold.patch").write_text(gold_patch, encoding="utf-8")
            (attempt / "test.patch").write_text(test_patch, encoding="utf-8")
            (attempt / "eval.sh").write_text(script, encoding="utf-8")
            encoded_gold = base64.b64encode(gold_patch.encode()).decode()

            def with_patch(encoded, inner):
                return ("cd /testbed && echo '" + encoded +
                        "' | base64 -d > /tmp/rf-change.patch && "
                        "git apply --check /tmp/rf-change.patch && git apply /tmp/rf-change.patch "
                        "|| exit 1\n" + inner)

            controls = {"noop": run_script(script),
                        "gold": run_script(with_patch(encoded_gold, script)),
                        "gold_clean_apply": run_script(with_patch(encoded_gold, "git diff --check && git status --short"))}
            for i, spec in enumerate(specs):
                partial = render_patch(ApiMigrationGoldGenerator().generate(repo, spec).changes)
                (attempt / f"partial-only-{i}.patch").write_text(partial, encoding="utf-8")
                encoded_partial = base64.b64encode(partial.encode()).decode()
                controls[f"partial_{i}"] = run_script(with_patch(encoded_partial, script))
                own_script = eval_script(test_patch, test_argv, f"test_rf_migration_{i}.py")
                controls[f"partial_{i}_own"] = run_script(with_patch(encoded_partial, own_script))
            for label, result in controls.items():
                write_json(attempt / f"{label}.json", {"returncode": result.returncode,
                           "stdout": result.stdout, "stderr": result.stderr})
            if (controls["noop"].returncode == 0 or controls["gold"].returncode != 0
                    or controls["gold_clean_apply"].returncode != 0
                    or any(controls[f"partial_{i}"].returncode == 0
                           or controls[f"partial_{i}_own"].returncode != 0
                           or f"test_rf_migration_{1-i}" not in controls[f"partial_{i}"].stderr
                           for i in range(len(specs)))):
                raise FactoryError("No-op, Gold, clean apply, or Partial Docker control failed")
            owner, name = repo_url.removesuffix(".git").rstrip("/").split("/")[-2:]
            instance_id = (f"{owner}__{name}-c_{base_commit[:7]}-"
                           f"{digest(':'.join(spec.candidate.candidate_id for spec in specs).encode())[:8]}")
            problem = ProblemGenerator().generate_compound(
                composition, Config(repo.snapshot, api_migrations=(ordered[0][0],),
                                    min_caller_modules=1, min_changed_files=2)).text
            row = {"instance_id": instance_id, "repo": f"{owner}/{name}",
                   "base_commit": base_commit, "environment_setup_commit": base_commit,
                   "created_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   "language": "python", "working_dir": "/testbed", "image_name": image_name,
                   "patch": gold_patch, "test_patch": test_patch,
                   "problem_statement": problem, "hints_text": ""}
            write_json(output / "swe-bench-promax.json", [row])
            write_json(output / "eval.json", {instance_id: {"instance_id": instance_id, "eval_script": script}})
            (output / "instances.jsonl").write_text(json.dumps({"instance_id": instance_id,
                "repository": {"snapshot": "source"}, "problem_statement": {"text": problem},
                "environment": {"python": ">=3.11", "dependencies": []}}, ensure_ascii=False) + "\n", encoding="utf-8")
            write_json(output / "composition.json", asdict(composition))
            write_json(output / "subtasks.json", [asdict(spec) for spec in specs])
            write_json(output / "validation.json", {label: {"returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr} for label, result in controls.items()})
            (output / "gold.patch").write_text(gold_patch, encoding="utf-8")
            (output / "test.patch").write_text(test_patch, encoding="utf-8")
            summary = {"status": "completed", "instance_id": instance_id,
                       "base_commit": base_commit, "image_name": image_name, "image_id": image_id,
                       "subtasks": [spec.candidate.symbol for spec in specs],
                       "controls": {label: result.returncode for label, result in controls.items()}}
            write_json(output / "summary.json", summary)
            return summary
        except (FactoryError, OSError, ValueError, SyntaxError) as exc:
            rejected.append({"pair": [pair[0].symbol, pair[1].symbol], "reason": str(exc)})
            write_json(output / "rejections.json", rejected)
    raise FactoryError("No related pair passed Docker compound controls")
