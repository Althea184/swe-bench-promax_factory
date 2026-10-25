# Refactoring Instance Factory

This repository is a research baseline for synthesizing SWE-Bench ProMax style refactoring instances. It supports local and real synthetic migrations, historical mining, compound composition, hybrid composition, and agent rollouts. It is not an official ProMax implementation.

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

## Code structure

`rf_factory` separates shared contracts (`core`), repository acquisition (`repository`), Docker execution (`environment`), historical and synthetic candidate discovery (`discovery`), Gold and task composition (`synthesis`), problem statements (`issue_gen`), instance and solution checks (`validation`), publication (`instances`), solver rollouts (`agents`), and route orchestration (`pipelines`). `pipelines/cli.py` implements the public commands; `__main__.py` preserves `python -m rf_factory`. The former top-level compatibility modules have been removed, so Python callers must import from the functional packages. Fixtures, prompts, CLI commands, and published data layouts remain in their established locations.

See [docs/architecture.md](docs/architecture.md) for the directory tree, dependency diagram, complete migration map, route call chains, and guidance for adding a refactoring strategy.

## Local pipeline

`pipelines/cli.py:main` dispatches to `pipelines/local.py:Pipeline.run` for `demo` and `run`:

1. `repository/snapshot.py:RepositoryPreparer.prepare` copies and hashes the source tree into `source/`.
2. `discovery/synthetic/` finds configured candidates and supported static usages, then ranks them and records rejection reasons.
3. `synthesis/generators/` constructs a `TaskSpec`, makes the Gold Patch, and applies it to a separate workspace.
4. `issue_gen/generator.py:ProblemGenerator.generate` derives the solver-facing statement from TaskSpec, without reading gold.
5. `validation/` builds the Oracle, runs the baseline and portable verifier, and requires Gold PASS / No-op FAIL.
6. `instances/` builds and publishes accepted instances; `environment/` handles Docker resources.

`core/models.py` defines stage records, `core/config.py` validates experiment settings, `core/common.py` handles safe serialization and hashes, and `validation/test_runner.py` is the portable unittest adapter. The local pipeline supports API migration and recipe-guided module responsibility migration. The latter consolidates equivalent, self-contained top-level functions from at least two caller modules into an existing service module, then redirects local calls through imports. Unsupported definitions are rejected during discovery. `run-real` remains the Docker-gated API migration MVP.

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

## Historical PR mining (opt in)

`run-history` obtains merged PR metadata and commit/file evidence from the GitHub API, resolves the actual base from the merged commit's Git ancestry, and checks the merged diff against the PR file list. It uses repository-independent AST rules to record extraction, decomposition, relocation, and interface evolution as macro labels and separate atomic operations. A strategy registry must cover every operation before Docker synthesis. The prior `canonicalize_version` behavior probe remains available; a generic structural strategy now checks extracted or relocated definitions, their production callers, and supported interface-default changes alongside the original regression suite. PR, commit, and issue URL entry points remain available. An issue without one traceable merged PR produces a preflight rejection without inventing a Gold Patch.

`history-scan` accepts a JSON list/object (`{"prs": [...]}`) or newline-separated PR URLs. It writes `candidates.json`, `rejections.json`, `preflight_reports.json`, and `preflight_summary.json` without Docker. GitHub API responses and repository Git objects are cached under `--cache` and reused by later scans or `run-history --cache`. A preflight-eligible candidate is **not** a verified Instance. See [the eight-PR experiment and exact reproduction commands](docs/historical-generalization.md).

Verified source: [pypa/packaging PR #793](https://github.com/pypa/packaging/pull/793), squash merge commit `680c31aa25d24f71f897717f84e3dac434a8c063`, base `c385b58c4d0e4d66f5c8273ebb155801c92aadf3`. The PR describes removing duplicate version-string rendering in `canonicalize_version`. Its real developer diff changes `src/packaging/utils.py` and `src/packaging/version.py`; the Gold Patch is that exact production diff. The acceptance patch checks that canonicalization uses the `Version` string renderer and retains version behavior, without comparing the solver patch to Gold. On Python 3.11 Docker, Baseline and Gold each passed 26,846 upstream tests (6 warnings); No-op failed two of three new acceptance checks; clean Gold application passed. The published instance is `runs-history-packaging-pr793-final-20261024/pypa__packaging-h_c385b58-pr793-c20a3bec/`.

```powershell
docker build -t rf-packaging-test-base:py311 -f docker/packaging.Dockerfile docker
python -m rf_factory run-history --pr-url https://github.com/pypa/packaging/pull/793 --preflight --output runs-history-pr793-preflight-repro
python -m rf_factory run-history --pr-url https://github.com/pypa/packaging/pull/793 --base-image rf-packaging-test-base:py311 --offline-build --test-command 'python -m pytest -q tests' --output runs-history-pr793-repro
```

Use a new `--output` on every rerun. A clean local GitHub clone can be supplied with `--local-path`; the verified run used `D:\class\remote\SJT_LLMSE\packaging`. Commit mode uses `--repo-url https://github.com/pypa/packaging --commit 680c31aa25d24f71f897717f84e3dac434a8c063`. The controller directory retains `source_pr.json`, `original-pr.patch`, `gold.patch`, `test.patch`, `problem_evidence.json`, `patch_provenance.json`, and complete Docker logs. `prepare-rollout` exports only the fixed base repository, task, and environment. Public GitHub history itself can reveal the developer solution to a solver that searches online; this provenance risk is recorded in `problem_evidence.json`.

`RefactoringCandidate` and `RefactoringTaskSpec` provide a source-neutral private representation. Historical mining creates them directly; the synthetic route projects its existing TaskSpec through `candidate_pool.py` and keeps the old schema and verifier. `history_composition.py` can plan two consecutive, related historical revisions in an isolated clone and produce one Base-to-final diff. It does not publish a compound instance until independent subtask acceptance and Partial controls are available. Cross-source composition also requires one reproducible base and remains a planned extension. Difficulty metadata describes task size and structure; it is not a measured Agent difficulty score.

`run-history-compound` composes two consecutive, structurally related historical refactorings from one GitHub repository. It accepts two merged PR URLs with API metadata, or two full commit SHAs with a GitHub repository URL and Git provenance. `--preflight` checks lineage, classification, relationship, and acceptance availability without Docker. The full run replays both revisions, derives one Base-to-final Gold diff, and requires independent subtask acceptance plus Docker Baseline, No-op, Gold, clean apply, and executable Partial controls. Private provenance and tests stay outside `prepare-rollout`. See [the HTTPX historical compound experiment](docs/historical-compound.md).

`run-hybrid-compound` reuses the two verified HTTPX historical relocations, then discovers and applies two dependent local API migrations. It exports one four-subtask Instance with separate developer and generated patch provenance, a single Base-to-final Gold Patch, independent checks, and Docker Partial controls. See [the HTTPX Hybrid experiment and reproduction command](docs/hybrid-compound.md).

After these changes, the existing `run-real --compound` route was also rerun against `peng-weihan/swe-series-factory` at `661d5b978a30e6d3517ad8232451353bb5e81ce1`. Discovery found 6 candidates and 5 eligible pairs. Its full Docker run passed Baseline and Gold, failed No-op and both incomplete Partial variants, and passed each Partial variant's own acceptance. The regression output is `runs-compound-swe-history-regression-full-20261024/peng-weihan__swe-series-factory-c_661d5b9-61ffa981/`.

## Compound API migration (opt in)

`run-real --compound` creates one instance from exactly two related, automatically discovered API migrations. The default `run-real` path is unchanged. The current relation rule accepts a direct call between APIs or shared production callers within a domain-specific API module. Generic `common`, `utils`, and `cli` modules need a direct call. Pair and candidate rejection reasons are retained in `rejections.json`. The generator applies each migration in an isolated checkout, refreshes the call graph after each step, and generates one Gold Patch from the base commit to the final tree. Each subtask gets its own acceptance file; one `test.patch` and `eval.sh` run those checks alongside the selected original regression suite. Docker controls cover Baseline, No-op, Gold, clean Gold application, and both one-subtask variants. A variant must pass its own acceptance check and fail the full acceptance check.

Verified public repository: `https://github.com/peng-weihan/swe-series-factory` at `661d5b978a30e6d3517ad8232451353bb5e81ce1`. The related `swe_factory.snapshots.acquire` and `changed` APIs both serve the DeepSWE snapshot workflow; the former is also used by ProgramBench. The selected original regression file is `test_factory_shared.py` (13 tests). The command below uses an already available base image with offline build dependencies. Omit `--local-path` to clone from GitHub when network access is available, or point it to a clean clone of that GitHub repository at the pinned commit.

```powershell
python -m rf_factory run-real --compound --repo-url https://github.com/peng-weihan/swe-series-factory --local-path D:\class\remote\SJT_LLMSE\swe-series-factory --commit 661d5b978a30e6d3517ad8232451353bb5e81ce1 --output runs-compound-delivery-20261024 --max-candidates 8 --base-image key4127/refactor-dockerhub:confident-ai__deepeval-c_08844c8 --offline-build --test-command 'python -m unittest discover -s tests -p test_factory_shared.py'
```

The published instance contains `swe-bench-promax.json`, `eval.json`, `gold.patch`, `test.patch`, `baseline.json`, `validation.json`, `composition.json`, `subtasks.json`, `relations.json`, `rejections.json`, and per-attempt control logs. Only the problem statement and base repository belong in a solver rollout; Gold, acceptance tests, composition details, and logs stay controller-side. This MVP supports two static Python API migrations only. Dynamic calls, arbitrary signatures, and other refactoring types remain unsupported. A selected regression suite that directly calls an API with its original positional signature disqualifies that candidate; the original tests are never rewritten.

## Agent Runner

`run-agent` runs a trusted external solver against one published Docker-backed real instance. It calls `prepare-rollout` for each repetition, runs the solver from the clean repository, collects a Git diff against the fixed base commit, rejects edits to test files, and applies the proposed patch in a fresh offline Docker container before running the instance's `eval.json` script. Compound runs also report each subtask's acceptance result after the full evaluation. It saves the solver's stdout/stderr, patch, prediction JSON, evaluation logs, per-attempt result, and a success-rate summary. Rollout workspaces are removed after each attempt. The controller-side Gold Patch, hidden acceptance tests, and composition metadata are not copied into the solver workspace.

```powershell
python -m rf_factory run-agent --run runs-compound-packaging-c1d2a9d-20261024/pypa__packaging-c_c1d2a9d-d7493939 --instance pypa__packaging-c_c1d2a9d-d7493939 --agent-command 'python path/to/my_agent.py' --model my-agent --max-turns 20 --repetitions 3 --agent-timeout 1800 --eval-timeout 1800 --output runs-agent-packaging-001
```

The command runs with its working directory set to the solver repository. The task text is provided on stdin and through `RF_TASK_FILE`; `RF_REPOSITORY`, `RF_RESULT_FILE`, `RF_MAX_TURNS`, and `RF_MODEL` are also set. The command string may use whole-token `{task}`, `{repo}`, `{max_turns}`, and `{model}` placeholders. An adapter may write `RF_RESULT_FILE` as JSON with nonnegative `turns`, `input_tokens`, `output_tokens`, and `cost_usd`. These usage values are reported by the adapter, not measured by the Factory. The Runner enforces wall-clock timeouts and rejects a reported turn count above `--max-turns`; enforcing the turn limit during generation is the adapter's responsibility. External commands run on the host and must be trusted; the rollout directory is a data boundary, not an operating-system security sandbox. Only ProMax real instances with an available pinned Docker image and `eval.json` are supported in this MVP.

For a `src/` layout such as `src/packaging/utils.py`, discovery uses the Python module name `packaging.utils` while patches retain the repository path. Relative imports such as `from .utils import name` are resolved against the importing package. Run the read-only discovery preflight before building an image; it reports every considered candidate, production call site, related pair, and rejection reason in `preflight.json`:

```powershell
python -m rf_factory run-real --compound --preflight --repo-url https://github.com/pypa/packaging --local-path D:\class\remote\SJT_LLMSE\packaging --commit c1d2a9d8f5ca7cc11b7d2e902ed5ee3e255e8560 --output runs-compound-packaging-preflight --max-candidates 100 --test-command 'python -m pytest -q tests'
```

`--local-path` may be omitted to clone from GitHub. Preflight uses a temporary checkout and does not build Docker or modify the source checkout. A nonzero exit means no eligible pair or a discovery error; inspect `preflight.json` and `summary.json` in the output directory. When a pair is eligible, run the full Docker pipeline with the same commit, candidate limit, and regression command. To use an offline image build, first provide a base image with Git, `flit_core`, `pytest`, `hypothesis`, `pretend`, `coverage`, and `tomli_w`; then pass its tag with `--base-image` and `--offline-build`. Full synthesis still requires Baseline, Gold, No-op, clean Gold application, and both Partial controls.
Each run requires a new `--output` directory; change the example directory names when repeating the experiment.

For the pinned packaging revision, a reproducible local base image and full run are:

```powershell
docker build -t rf-packaging-test-base:py311 -f docker/packaging.Dockerfile docker
python -m rf_factory run-real --compound --repo-url https://github.com/pypa/packaging --local-path D:\class\remote\SJT_LLMSE\packaging --commit c1d2a9d8f5ca7cc11b7d2e902ed5ee3e255e8560 --output runs-compound-packaging-c1d2a9d-20261024 --max-candidates 100 --base-image rf-packaging-test-base:py311 --offline-build --test-command 'python -m pytest -q tests'
```

Compound acceptance files live in `rf_acceptance/` inside the test patch. The original `tests/` regression suite runs in full, and each Partial control can run exactly one acceptance file to show that its own subtask is complete while the compound acceptance remains incomplete.

Verified on the pinned packaging commit: preflight found four eligible API candidates and four related pairs. The selected pair is `packaging._ranges.range_is_empty` and `packaging._ranges.intersect_ranges`: the latter calls the former, and both serve range operations used by `packaging.ranges`. The exported instance is `runs-compound-packaging-c1d2a9d-20261024/pypa__packaging-c_c1d2a9d-d7493939/`. Docker Baseline and Gold each passed 63,335 upstream tests (3 skipped, 434 deselected by upstream configuration); No-op failed all four generated acceptance checks; each one-subtask Partial passed its own two checks and failed the other subtask's two checks. The final Gold Patch applied cleanly to the base image. See `baseline.json`, `validation.json`, and `attempts/0/` for full logs. The generated tests check the named-argument contract and tracked static callers; dynamic invocation patterns remain outside this MVP.

The real module-responsibility example uses the previously frozen `peng-weihan/swe-series-factory` source at commit `661d5b978a30e6d3517ad8232451353bb5e81ce1`. It consolidates identical `Pipeline.event` methods in two factory modules into `swe_factory.reports`. The selected original regression file has five passing tests on Windows; the generated verifier additionally calls both event methods and checks stage tracking and `stages.json`. The local pipeline exports `rf.instance.v3`, not the ProMax-compatible `run-real` schema. This example needs the frozen source directory named in its config, and on Windows may require access to the system temporary directory for upstream tests.
