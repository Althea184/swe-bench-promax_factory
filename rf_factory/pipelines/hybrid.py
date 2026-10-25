"""Orchestrate the existing historical plus synthetic Hybrid route."""
import ast
import base64
import datetime as dt
import json
import shlex
import shutil
from dataclasses import asdict
from pathlib import Path
from ..core.common import FactoryError, digest, write_json
from ..core.config import Config, MigrationRecipe
from ..core.models import CompoundTaskSpec
from ..core.process import command, require
from ..discovery.synthetic.api_migration import ApiMigrationDiscovery
from ..synthesis.generators.dependent_migrations import _local_migrations
from ..discovery.synthetic.usage_analysis import module_name
from ..pipelines.historical_compound import HistoryCompoundPipeline
from ..repository import RepositoryPreparer
from ..synthesis.candidate_pool import difficulty_metadata, synthetic_candidate
from ..synthesis.composition.hybrid import compose_hybrid
from ..synthesis.generators.api_migration import ApiMigrationGoldGenerator, ApiMigrationSpecBuilder
from ..validation.historical_acceptance import StaticStructuralOperations
from ..validation.hybrid_controls import _result, _with_patch
from ..validation.synthetic_acceptance import eval_script, test_patch_for
from ..validation.instance import hybrid_controls_valid


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
            if not hybrid_controls_valid(controls, len(prefixes)):
                raise FactoryError("Hybrid Docker controls failed")
            stage = "export"
            unified = [synthetic_candidate(spec, repository=repo_url, base_commit=base,
                                           files=repo.files) for spec in synthetic_specs]
            # The historical task contracts retain their original developer provenance.
            from ..synthesis.generators.historical import discover_history
            from ..repository.git_history import commit_record
            source = local_path or history_dir / "acquired"
            historical_tasks = [discover_history(commit_record(source, repo_url, sha), source)[0]
                                for sha in commits]
            tasks = [*historical_tasks, *unified]
            composition, edges, scope, goal = compose_hybrid(tasks, commits, synthetic_steps)
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
