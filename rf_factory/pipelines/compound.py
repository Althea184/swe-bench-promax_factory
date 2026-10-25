"""Orchestrate existing synthetic compound controls and publication."""
import base64
import datetime as dt
import json
from dataclasses import asdict
from ..core.common import FactoryError, digest, write_json
from ..core.config import Config
from ..core.models import CompoundTaskSpec
from ..core.process import command, require
from ..issue_gen.generator import ProblemGenerator
from ..synthesis.candidate_pool import synthetic_candidate, difficulty_metadata
from ..synthesis.composition.synthetic import candidates_for, apply_steps
from ..synthesis.generators.api_migration import ApiMigrationGoldGenerator, render_patch
from ..validation.synthetic_acceptance import eval_script, test_patch_for
from ..validation.instance import synthetic_compound_controls_valid


def synthesize_compound(repo, recipes, output, clone, repo_url, base_commit,
                        image_name, image_id, test_argv, run_script):
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
            test_patch = "".join(test_patch_for(spec, f"rf_acceptance/test_rf_migration_{i}.py")
                                 for i, spec in enumerate(specs))
            script = eval_script(test_patch, test_argv, "test_rf_migration_*.py", "rf_acceptance")
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
                own_script = eval_script(test_patch, test_argv, f"test_rf_migration_{i}.py",
                                         "rf_acceptance")
                controls[f"partial_{i}_own"] = run_script(with_patch(encoded_partial, own_script))
            for label, result in controls.items():
                write_json(attempt / f"{label}.json", {"returncode": result.returncode,
                           "stdout": result.stdout, "stderr": result.stderr})
            if not synthetic_compound_controls_valid(controls, len(specs)):
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
            unified_tasks = [synthetic_candidate(spec, repository=repo_url,
                             base_commit=base_commit, files=repo.files) for spec in specs]
            write_json(output / "unified_candidates.json",
                       [asdict(task.candidate) for task in unified_tasks])
            write_json(output / "difficulty.json", difficulty_metadata(
                       unified_tasks, gold_patch, [path for step in steps for path in step["changed_files"]],
                       pair[4]))
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
