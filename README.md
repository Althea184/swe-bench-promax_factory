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
python -m rf_factory run --config examples/module-responsibility.toml --output runs
python -m rf_factory run --config examples/real-module-responsibility.toml --output runs-responsibility
python -m rf_factory run-real --repo-url https://github.com/owner/repo --commit <full-commit-sha> --output runs
python -m rf_factory run-real --compound --repo-url https://github.com/owner/repo --commit <full-commit-sha> --output runs
python -m rf_factory prepare-rollout --run runs/<instance-id> --instance <instance-id> --output <separate-solver-directory>
python -m rf_factory export-base --run runs/<instance-id> --output <shared-base-image.tar>
python -m pytest tests -q
```

`--output` names the instance collection directory. Each successful instance is published to `<output>/<instance-id>/`; an existing instance directory is never overwritten. Generation evidence is staged in `.rf-factory-work/` beside the collection, keeping incomplete attempts out of `runs/`. A local batch stores its source once in `<output>/_bases/<content-hash>/source/`; published instances reference that base and omit generation workspaces. The base image is built once when Docker is available. `demo` runs the packaged API migration fixture. `run` reads a TOML config; `repository.source` is relative to that config. The console script is `rf-factory`.

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

`models.py` defines stage records, `config.py` validates experiment settings, `common.py` handles safe serialization and hashes, and `test_runner.py` is the portable unittest adapter. The local pipeline supports API migration and recipe-guided module responsibility migration. The latter consolidates equivalent, self-contained top-level functions from at least two caller modules into an existing service module, then redirects local calls through imports. Unsupported definitions are rejected during discovery. `run-real` remains the Docker-gated API migration MVP.

## Problem statement policy

The current generator uses **medium abstraction**. It describes the goal, affected functionality, behavior, and compatibility, while omitting file inventories, call-site lists, exact signature edits, and step-by-step instructions. `prompts/problem_statement.md` records the policy and examples for future model-backed generation. A lightweight check rejects vague text, paths, exact API transformations, and procedural wording. The check is a guardrail, not a semantic judge.

## Artifacts

Each run retains stage evidence:

```text
<output>/<instance-id>/
  config.json  runtime.json  repository.json
  candidates.json  discovery_diagnostics.json  selected_candidates.json  candidate_rejections.json
  baseline/{plan.json,result.json,tests.json,stdout.txt,stderr.txt}
  tasks/<id>/{spec.json,problem_statement.json,oracle.json,difficulty_metadata.json}
  task_specs.json
  gold/<id>/{gold.patch,patch.json,application.json}
  verifier/<id>/{runner.py,usage_analysis.py,test_runner.py,spec.json}
  precheck/<id>/{gold.json,noop.json,result.json}
  rejections.json  instances.json  instances.jsonl
  docker/<id>/Dockerfile  Dockerfile  .dockerignore  docker.json  summary.json
```

`instances.jsonl` contains one controller-side instance per directory. Published local instances reference a shared source base; real GitHub instances reference a pinned Docker image containing the fixed commit. `prepare-rollout` materializes a solver workspace from that base. Give the solver only the exported repository and task; keep TaskSpec, oracle, gold, and precheck private. `export-base` saves an available image once for offline transfer; load it with `docker image load` on the evaluator. If Docker is unavailable, local instances can still use the shared source for rollout, while real GitHub instances require their pinned image for offline rollout and evaluation.

The current verifier recognizes direct imports, module-qualified access, and static re-exports, and allows implementations whose bytes differ from gold. It is not a proof of full behavioral equivalence. See [docs/reference-analysis.md](docs/reference-analysis.md) for the architectural comparison, [docs/pilot.md](docs/pilot.md) for the smoke fixture, and [docs/harder-batch.md](docs/harder-batch.md) for the five-task batch and rollout instructions.

`run-real` is the Docker-gated Python MVP for fixed GitHub revisions. It discovers a conservative API migration from source, emits a Gold Patch and Test Patch, requires container baseline/gold/no-op checks, and exports ProMax-compatible `swe-bench-promax.json` plus `eval.json`. See [docs/real-mvp.md](docs/real-mvp.md) for the verified run, output layout, commands, and limitations.

## Compound API migration (opt in)

`run-real --compound` creates one instance from exactly two related, automatically discovered API migrations. The default `run-real` path is unchanged. The current relation rule accepts a direct call between APIs or shared production callers within a domain-specific API module. Generic `common`, `utils`, and `cli` modules need a direct call. Pair and candidate rejection reasons are retained in `rejections.json`. The generator applies each migration in an isolated checkout, refreshes the call graph after each step, and generates one Gold Patch from the base commit to the final tree. Each subtask gets its own acceptance file; one `test.patch` and `eval.sh` run those checks alongside the selected original regression suite. Docker controls cover Baseline, No-op, Gold, clean Gold application, and both one-subtask variants. A variant must pass its own acceptance check and fail the full acceptance check.

Verified public repository: `https://github.com/peng-weihan/swe-series-factory` at `661d5b978a30e6d3517ad8232451353bb5e81ce1`. The related `swe_factory.snapshots.acquire` and `changed` APIs both serve the DeepSWE snapshot workflow; the former is also used by ProgramBench. The selected original regression file is `test_factory_shared.py` (13 tests). The command below uses an already available base image with offline build dependencies. Omit `--local-path` to clone from GitHub when network access is available, or point it to a clean clone of that GitHub repository at the pinned commit.

```powershell
python -m rf_factory run-real --compound --repo-url https://github.com/peng-weihan/swe-series-factory --local-path D:\class\remote\SJT_LLMSE\swe-series-factory --commit 661d5b978a30e6d3517ad8232451353bb5e81ce1 --output runs-compound-delivery-20261024 --max-candidates 8 --base-image key4127/refactor-dockerhub:confident-ai__deepeval-c_08844c8 --offline-build --test-command 'python -m unittest discover -s tests -p test_factory_shared.py'
```

The published instance contains `swe-bench-promax.json`, `eval.json`, `gold.patch`, `test.patch`, `baseline.json`, `validation.json`, `composition.json`, `subtasks.json`, `relations.json`, `rejections.json`, and per-attempt control logs. Only the problem statement and base repository belong in a solver rollout; Gold, acceptance tests, composition details, and logs stay controller-side. This MVP supports two static Python API migrations only. Dynamic calls, arbitrary signatures, and other refactoring types remain unsupported. A selected regression suite that directly calls an API with its original positional signature disqualifies that candidate; the original tests are never rewritten.

The real module-responsibility example uses the previously frozen `peng-weihan/swe-series-factory` source at commit `661d5b978a30e6d3517ad8232451353bb5e81ce1`. It consolidates identical `Pipeline.event` methods in two factory modules into `swe_factory.reports`. The selected original regression file has five passing tests on Windows; the generated verifier additionally calls both event methods and checks stage tracking and `stages.json`. The local pipeline exports `rf.instance.v3`, not the ProMax-compatible `run-real` schema. This example needs the frozen source directory named in its config, and on Windows may require access to the system temporary directory for upstream tests.
