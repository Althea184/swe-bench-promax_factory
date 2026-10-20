# Refactoring Instance Factory

This repository is a research baseline for synthesizing SWE-Bench ProMax style refactoring instances. API migration is the first narrow, deterministic strategy used to exercise the complete pipeline. It is not an official ProMax implementation.

## Run

Python 3.11+ is required. Run only on trusted local Python repositories: verification executes repository tests on the host.

```powershell
python -m pip install -e .
python -m rf_factory demo --output runs
python -m rf_factory validate-config --config examples/local.toml
python -m rf_factory run --config examples/local.toml --output runs
python -m rf_factory run --config examples/harder-api-migration.toml --output runs
python -m rf_factory run-real --repo-url https://github.com/owner/repo --commit <full-commit-sha> --output runs
python -m rf_factory prepare-rollout --run runs/<instance-id> --instance <instance-id> --output <separate-solver-directory>
python -m pytest tests -q
```

`--output` names the instance collection directory. Each successful instance is published to `<output>/<instance-id>/`; an existing instance directory is never overwritten. Generation evidence is staged in `.rf-factory-work/` beside the collection, keeping incomplete attempts out of `runs/`. `demo` runs the packaged API migration fixture. `run` reads a TOML config; `repository.source` is relative to that config. The console script is `rf-factory`.

## The one pipeline

`cli.py:main` calls `pipeline.py:Pipeline.run`:

1. `repository.py:RepositoryPreparer.prepare` copies and hashes the source tree into `source/`.
2. `api_migration.py:ApiMigrationDiscovery.discover` finds configured API candidates and supported static usages; `candidates.py:filter_and_rank` selects them and records rejection reasons.
3. `api_migration.py:ApiMigrationSpecBuilder.build` constructs a structured `TaskSpec`. Candidate means opportunity; TaskSpec means the internal task contract.
4. `api_migration.py:ApiMigrationGoldGenerator.generate` makes a reference patch; `PatchApplier.apply` checks hashes and applies it to a separate workspace.
5. `problem_generation.py:ProblemGenerator.generate` derives the solver-facing statement from TaskSpec, without reading gold.
6. `oracle.py:OracleBuilder.build` derives acceptance conditions from TaskSpec and source hashes. `BaselineOracle` confirms the original tests pass.
7. `verifier.py:verify` runs structural, regression, and functional checks on both gold and unchanged workspaces.
8. `evaluator.py:Evaluator` scores the observations and requires gold PASS / no-op FAIL.
9. `instance_builder.py:InstanceBuilder.build` packages accepted tasks; `pipeline.py` exports JSON and JSONL.

`models.py` defines stage records, `config.py` validates experiment settings, `common.py` handles safe serialization and hashes, and `test_runner.py` is the portable unittest adapter. The API migration strategy is currently the only implemented refactoring type; configuration rejects unsupported strategies rather than silently substituting a different one.

## Problem statement policy

The current generator uses **medium abstraction**. It describes the goal, affected functionality, behavior, and compatibility, while omitting file inventories, call-site lists, exact signature edits, and step-by-step instructions. `prompts/problem_statement.md` records the policy and examples for future model-backed generation. A lightweight check rejects vague text, paths, exact API transformations, and procedural wording. The check is a guardrail, not a semantic judge.

## Artifacts

Each run retains stage evidence:

```text
<output>/<instance-id>/
  config.json  runtime.json  repository.json  source/
  candidates.json  discovery_diagnostics.json  selected_candidates.json  candidate_rejections.json
  baseline/{plan.json,result.json,tests.json,stdout.txt,stderr.txt,workspace/}
  tasks/<id>/{spec.json,problem_statement.json,oracle.json,difficulty_metadata.json}
  task_specs.json
  gold/<id>/{gold.patch,patch.json,application.json,workspace/}
  verifier/<id>/{runner.py,usage_analysis.py,test_runner.py,spec.json}
  precheck/<id>/{gold.json,noop.json,result.json,noop_workspace/}
  rejections.json  instances.json  instances.jsonl
  docker/<id>/Dockerfile  Dockerfile  .dockerignore  docker.json  summary.json
```

`instances.jsonl` contains one controller-side instance per directory. Each instance directory is a reproducible experiment bundle, including a source snapshot and its own evidence. Give a solver only `source/` and `problem_statement.text`; keep TaskSpec, oracle, gold, and precheck private. `Dockerfile` and `docker.json` record the optional container check; host verification still runs when Docker is unavailable.

The current verifier recognizes direct imports, module-qualified access, and static re-exports, and allows implementations whose bytes differ from gold. It is not a proof of full behavioral equivalence. See [docs/reference-analysis.md](docs/reference-analysis.md) for the architectural comparison, [docs/pilot.md](docs/pilot.md) for the smoke fixture, and [docs/harder-batch.md](docs/harder-batch.md) for the five-task batch and rollout instructions.

`run-real` is the Docker-gated Python MVP for fixed GitHub revisions. It discovers a conservative API migration from source, emits a Gold Patch and Test Patch, requires container baseline/gold/no-op checks, and exports ProMax-compatible `swe-bench-promax.json` plus `eval.json`. See [docs/real-mvp.md](docs/real-mvp.md) for the verified run, output layout, commands, and limitations.
