"""Compose verified historical relocations with dependent local API migrations."""
from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import shlex
import shutil
from dataclasses import asdict
from pathlib import Path

from .api_migration import ApiMigrationDiscovery, ApiMigrationGoldGenerator, ApiMigrationSpecBuilder
from .candidate_pool import difficulty_metadata, synthetic_candidate
from .common import FactoryError, digest, write_json
from .config import Config, MigrationRecipe
from .history_acceptance import StaticStructuralOperations
from .history_compound import HistoryCompoundPipeline
from .models import CompoundTaskSpec
from .real_pipeline import command, eval_script, require, test_patch_for
from .repository import RepositoryPreparer
from .usage_analysis import module_name


def _result(process):
    return {"returncode": process.returncode, "stdout": process.stdout,
            "stderr": process.stderr}


def _with_patch(patch, script):
    encoded = base64.b64encode(patch.encode()).decode()
    return ("cd /testbed && echo '" + encoded + "' | base64 -d > /tmp/rf-change.patch && "
            "git apply --check /tmp/rf-change.patch && git apply /tmp/rf-change.patch || exit 1\n" +
            script)


def _local_migrations(repo, historical_specs):
    """Find supported two-argument helpers created by each historical relocation."""
    proposals, rejected = [], []
    for historical in historical_specs:
        options = []
        for operation in historical["candidate"]["evidence"]["atomic_operations"]:
            if operation["kind"] != "relocation":
                continue
            path, symbol = operation["to_module"], operation["to_symbol"]
            module = module_name(path, repo.files)
            try:
                tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
                matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name == symbol]
                if len(matches) != 1 or len(matches[0].args.args) != 2:
                    raise ValueError("relocated helper is not a supported two-argument function")
                parameter = matches[0].args.args[1].arg
                recipe = MigrationRecipe(module, symbol, symbol, parameter,
                    f"Require {parameter} by name when calling the relocated {symbol} helper.",
                    ("Preserve header and multipart behavior.",),
                    ("Update all production callers of the helper.",), (), auto_discovered=True)
                config = Config(repo.snapshot, api_migrations=(recipe,),
                    min_caller_modules=1, min_changed_files=1, include_local_calls=True)
                candidates, diagnostics = ApiMigrationDiscovery().discover(repo, config)
                if not candidates:
                    raise ValueError(f"API migration discovery rejected helper: {diagnostics}")
                spec = ApiMigrationSpecBuilder().build(repo, candidates[0], config)
                local_count = spec.metadata["call_counts"].get(path, 0)
                if not local_count:
                    raise ValueError("relocated helper has no local production calls")
                options.append((local_count, symbol, recipe, spec, operation))
            except (OSError, SyntaxError, ValueError, FactoryError) as exc:
                rejected.append({"module": module, "symbol": symbol, "reason": str(exc)})
        if not options:
            raise FactoryError("Historical subtask has no dependent local API migration")
        options.sort(key=lambda item: (-item[0], item[1]))
        chosen = options[0]
        proposals.append((chosen[2], chosen[4]))
        rejected.extend({"module": module_name(item[4]["to_module"], repo.files),
                         "symbol": item[1], "reason": "lower-ranked eligible migration"}
                        for item in options[1:])
    return proposals, rejected


class HybridCompoundPipeline:
    def run(self, *, output: Path, commits: list[str], repo_url: str,
            local_path: Path | None = None, base_image="python:3.11-slim",
            offline_build=False, test_argv=None):
        if len(commits) != 2:
            raise FactoryError("Hybrid MVP requires two consecutive historical commits")
        output = output.resolve()
        if output.exists():
            raise FactoryError(f"Output already exists: {output}")
        output.mkdir(parents=True)
        test_argv = test_argv or ["python", "-m", "pytest", "-q", "tests"]
        stage = "history"
        try:
            history_dir = output / "historical-stage"
            history = HistoryCompoundPipeline().run(output=history_dir, commits=commits,
                repo_url=repo_url, local_path=local_path, base_image=base_image,
                offline_build=offline_build, test_argv=test_argv)
            historical_specs = json.loads((history_dir / "subtasks.json").read_text(encoding="utf-8"))
            base = history["base_commit"]
            historical_gold = (history_dir / "gold.patch").read_text(encoding="utf-8")
            workspace = output / "workspace"
            shutil.copytree(history_dir / "acquired", workspace)
            historical_prefix = []
            for index in range(2):
                patch = (history_dir / f"original-{index}.patch").read_text(encoding="utf-8")
                patch_path = output / f"historical-step-{index}.patch"
                patch_path.write_bytes(patch.encode())
                require(command(["git", "apply", "--check", str(patch_path)], cwd=workspace),
                        f"Historical step {index} conflict check")
                require(command(["git", "apply", str(patch_path)], cwd=workspace),
                        f"Historical step {index} replay")
                prefix = require(command(["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
                                         cwd=workspace), "Historical prefix diff") + "\n"
                historical_prefix.append(prefix)
            replayed = require(command(["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
                                       cwd=workspace), "Historical Gold replay") + "\n"
            if replayed.encode() != historical_gold.encode():
                raise FactoryError("Historical replay does not match verified developer Gold")
            stage = "synthetic_discovery"
            repo = RepositoryPreparer().prepare(workspace, output / "post-history-source")
            proposals, rejections = _local_migrations(repo, historical_specs)
            write_json(output / "discovery.json", {"proposals": [
                {"module": recipe.module, "symbol": recipe.symbol,
                 "parameter": recipe.parameter, "depends_on": operation}
                for recipe, operation in proposals], "rejections": rejections})
            stage = "synthetic_replay"
            synthetic_specs, synthetic_steps = [], []
            prefixes = list(historical_prefix)
            for index, (recipe, operation) in enumerate(proposals):
                current = RepositoryPreparer().prepare(workspace, output / f"synthetic-{index}-source")
                config = Config(current.snapshot, api_migrations=(recipe,),
                    min_caller_modules=1, min_changed_files=1, include_local_calls=True)
                candidates, diagnostics = ApiMigrationDiscovery().discover(current, config)
                if not candidates:
                    raise FactoryError(f"Dependent migration rediscovery failed: {diagnostics}")
                spec = ApiMigrationSpecBuilder().build(current, candidates[0], config)
                gold = ApiMigrationGoldGenerator().generate(current, spec)
                if not gold.changes:
                    raise FactoryError("Dependent migration produced no changes")
                for change in gold.changes:
                    (workspace / change.path).write_bytes(change.after.encode("utf-8"))
                after = RepositoryPreparer().prepare(workspace, output / f"synthetic-{index}-after")
                updated, _ = ApiMigrationDiscovery().discover(after, config)
                if updated:
                    raise FactoryError("Migrated helper still satisfies pre-migration discovery")
                synthetic_specs.append(spec)
                synthetic_steps.append({"symbol": recipe.symbol, "module": recipe.module,
                    "parameter": recipe.parameter, "changed_files": [c.path for c in gold.changes],
                    "callers": spec.metadata["call_counts"], "depends_on": operation["to_symbol"],
                    "generated_patch_sha256": gold.patch_sha256})
                prefixes.append(require(command(["git", "diff", "--binary", "--no-ext-diff",
                    "HEAD"], cwd=workspace), "Hybrid prefix diff") + "\n")
            full_gold = prefixes[-1]
            require(command(["git", "diff", "--check"], cwd=workspace), "Hybrid diff check")
            stage = "acceptance"
            acceptance = StaticStructuralOperations()
            historical_tests = [acceptance._test_patch(
                historical_specs[i]["candidate"]["evidence"]["atomic_operations"],
                f"rf_acceptance/test_rf_history_{i}.py", f"test_subtask_{i}_operations")
                for i in range(2)]
            synthetic_tests = [test_patch_for(spec, f"rf_acceptance/test_rf_migration_{i+2}.py",
                accepted_symbols=(spec.entities["target_api"],
                                  proposals[i][1]["from_symbol"]))
                for i, spec in enumerate(synthetic_specs)]
            test_patch = "".join((*historical_tests, *synthetic_tests))
            script = eval_script(test_patch, test_argv, "test_rf_*.py", "rf_acceptance")
            (output / "gold.patch").write_bytes(full_gold.encode())
            (output / "test.patch").write_bytes(test_patch.encode())
            (output / "eval.sh").write_bytes(script.encode())
            environment = json.loads((history_dir / "environment.json").read_text(encoding="utf-8"))
            image_id = environment["image_id"]
            def run_script(value):
                return command(["docker", "run", "--rm", "--network", "none", image_id,
                                "bash", "-c", value], timeout=600)
            controls = {"baseline": run_script("cd /testbed && export PYTHONPATH=/testbed && " +
                                                shlex.join(test_argv)),
                        "noop": run_script(script),
                        "gold": run_script(_with_patch(full_gold, script)),
                        "gold_clean_apply": run_script(_with_patch(full_gold,
                            "git diff --check && git status --porcelain"))}
            for index, prefix in enumerate(prefixes[:-1]):
                controls[f"partial_{index}"] = run_script(_with_patch(prefix, script))
                own_tests = "".join((*historical_tests, *synthetic_tests)[:index + 1])
                own_script = eval_script(own_tests, test_argv, "test_rf_*.py", "rf_acceptance")
                controls[f"partial_{index}_own"] = run_script(_with_patch(prefix, own_script))
            # The second generated migration is independent of the first once
            # its historical relocation exists. Check that skipping the first
            # generated goal cannot pass the complete contract either.
            alternate = output / "partial-without-2-workspace"
            shutil.copytree(history_dir / "acquired", alternate)
            historical_path = output / "historical-gold.patch"
            historical_path.write_bytes(historical_gold.encode())
            require(command(["git", "apply", str(historical_path)], cwd=alternate),
                    "Independent generated partial historical replay")
            second_recipe = proposals[1][0]
            second_config = Config(repo.snapshot, api_migrations=(second_recipe,),
                min_caller_modules=1, min_changed_files=1, include_local_calls=True)
            second_candidates, _ = ApiMigrationDiscovery().discover(repo, second_config)
            second_spec = ApiMigrationSpecBuilder().build(repo, second_candidates[0], second_config)
            second_gold = ApiMigrationGoldGenerator().generate(repo, second_spec)
            for change in second_gold.changes:
                (alternate / change.path).write_bytes(change.after.encode("utf-8"))
            alternate_patch = require(command(["git", "diff", "--binary", "--no-ext-diff",
                "HEAD"], cwd=alternate), "Independent generated partial diff") + "\n"
            (output / "partial-without-2.patch").write_bytes(alternate_patch.encode())
            controls["partial_without_2"] = run_script(_with_patch(alternate_patch, script))
            own_alternate = eval_script("".join((*historical_tests, synthetic_tests[1])),
                                        test_argv, "test_rf_*.py", "rf_acceptance")
            controls["partial_without_2_own"] = run_script(_with_patch(alternate_patch,
                                                                         own_alternate))
            write_json(output / "validation.json", {key: _result(value)
                                                       for key, value in controls.items()})
            if (controls["baseline"].returncode != 0 or controls["noop"].returncode == 0 or
                controls["gold"].returncode != 0 or
                controls["gold_clean_apply"].returncode != 0 or
                any(controls[f"partial_{i}"].returncode == 0 or
                    controls[f"partial_{i}_own"].returncode != 0
                    for i in range(len(prefixes) - 1)) or
                controls["partial_without_2"].returncode == 0 or
                controls["partial_without_2_own"].returncode != 0):
                raise FactoryError("Hybrid Docker controls failed")
            stage = "export"
            unified = [synthetic_candidate(spec, repository=repo_url, base_commit=base,
                                           files=repo.files) for spec in synthetic_specs]
            # The historical task contracts retain their original developer provenance.
            from .history import discover_history
            from .history_compound import commit_record
            source = local_path or history_dir / "acquired"
            historical_tasks = [discover_history(commit_record(source, repo_url, sha), source)[0]
                                for sha in commits]
            tasks = [*historical_tasks, *unified]
            ids = [task.candidate.candidate_id for task in tasks]
            edges = ((ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[3]))
            scope = tuple(sorted(set().union(*(set(task.scope) for task in tasks))))
            goal = ("Move HTTP header normalization and multipart form formatting out of the "
                    "shared utility module into their owning modules. Preserve existing behavior "
                    "and update production callers. Once relocated, require the header key "
                    "normalizer's encoding and the multipart form parameter formatter's value "
                    "to be supplied by name at every production call site.")
            composition = CompoundTaskSpec(tuple(tasks), edges, goal, scope,
                {"source_types": ["historical", "historical", "synthetic", "synthetic"],
                 "relation": "relocation enables local interface evolution",
                 "historical_commits": commits, "synthetic_steps": synthetic_steps})
            write_json(output / "composition.json", asdict(composition))
            write_json(output / "subtasks.json", [asdict(task) for task in tasks])
            write_json(output / "relations.json", {"edges": [
                {"from": a, "to": b, "basis": ("historical replay order" if i == 0 else
                 "destination helper must exist before its interface evolves")}
                for i, (a, b) in enumerate(edges)]})
            write_json(output / "patch_provenance.json", {
                "historical_patch_sha256": digest(historical_gold.encode()),
                "generated_steps": synthetic_steps, "unified_gold_sha256": digest(full_gold.encode()),
                "gold_source": "developer historical patches plus generated API migrations",
                "unified_diff": "one Base-to-final Git diff; source patches are not concatenated"})
            difficulty = difficulty_metadata(tasks, full_gold, scope,
                {"direct_dependencies": list(edges)})
            difficulty.update(operation_dependency_count=len(edges),
                generated_local_call_sites=sum(sum(step["callers"].values())
                                               for step in synthetic_steps),
                historical_relocation_count=sum(sum(op["kind"] == "relocation" for op in
                    task["candidate"]["evidence"]["atomic_operations"])
                    for task in historical_specs))
            write_json(output / "difficulty.json", difficulty)
            write_json(output / "environment.json", environment)
            write_json(output / "repository.json", json.loads(
                (history_dir / "repository.json").read_text(encoding="utf-8")))
            owner, name = repo_url.rstrip("/").removesuffix(".git").split("/")[-2:]
            identity = digest((base + ":" + ":".join(commits) + ":" +
                               ":".join(step["symbol"] for step in synthetic_steps)).encode())[:8]
            instance_id = f"{owner}__{name}-hy_{base[:7]}-{identity}"
            row = {"instance_id": instance_id, "repo": f"{owner}/{name}",
                   "base_commit": base, "environment_setup_commit": base,
                   "created_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   "language": "python", "working_dir": "/testbed", "image_name":
                   environment["image_name"], "patch": full_gold, "test_patch": test_patch,
                   "problem_statement": goal, "hints_text": ""}
            write_json(output / "swe-bench-promax.json", [row])
            write_json(output / "eval.json", {instance_id: {"instance_id": instance_id,
                       "eval_script": script}})
            (output / "instances.jsonl").write_text(json.dumps({"instance_id": instance_id,
                "repository": {"snapshot": "source"}, "problem_statement": {"text": goal},
                "environment": {"python": ">=3.11", "dependencies": []}}) + "\n",
                encoding="utf-8")
            summary = {"status": "completed", "instance_id": instance_id,
                "repository": repo_url, "base_commit": base, "image_id": image_id,
                "subtask_count": len(tasks), "controls": {key: value.returncode
                    for key, value in controls.items()}}
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "summary.json", {"status": "failed", "stage": stage,
                                                  "error": str(exc)})
            raise
