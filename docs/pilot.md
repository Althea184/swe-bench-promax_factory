# Multi-file API migration pilot (MVP2)

Run from the project root with Python 3.11+:

```powershell
python -m rf_factory pilot --output runs/pilot-001
python -m unittest discover -s tests -v
```

Use a new output directory for every run. `pilot --config examples/local.toml --output runs/my-pilot` accepts a trusted local Python repository. The deterministic strategy currently recognizes a module-level `render_amount(amount, currency)` and two or more modules that directly import and call it with two positional arguments. It changes the API to `format_amount(amount, *, currency)` and migrates each planned call. This deliberately narrow strategy avoids external LLM calls and historical commits. The existing `demo` and `run` commands retain their original MVP behavior.

The pilot pipeline is `Repository -> Candidate -> RefactoringPlan -> GoldPatch -> copied gold workspace -> Evaluator -> gold/no-op precheck -> Task -> v2 Instance -> JSONL`. The `BaselineOracle` remains the original repository regression check. It is separate from the gold solution precheck.

The evaluator checks the new keyword-only API, removal of the old definition, direct-import and call migration in every planned file, changes relative to the base hashes, original regression tests, and functional calls to the new API. A full score requires every check to pass. Gold must score 1.0 and the unchanged base must score 0.0; otherwise the candidate is rejected. This is a pilot acceptance gate, not a proof of semantic equivalence.

Each run keeps `source/`, `candidates.json`, `plans.json`, `gold/<candidate-id>/gold.patch`, `gold/<candidate-id>/patch.json`, `gold/<candidate-id>/workspace/`, `evaluator/<candidate-id>/runner.py`, `evaluator/<candidate-id>/spec.json`, `precheck/<candidate-id>/oracle.json`, `precheck/<candidate-id>/noop.json`, `instances.json`, `instances.jsonl`, `Dockerfile`, `docker.json`, and `summary.json`. The patch is a standard unified diff and its preimage/postimage hashes are checked before application. The copied gold workspace never replaces `source/`.

The v2 JSONL row contains `repository` (run-relative base snapshot), `task` (solver-facing statement), `plan`, `gold_patch` (artifact path, files, line counts and transformation metadata), `evaluator` (run-relative runner/spec and command), `precheck` (gold/no-op results), and `environment` (Dockerfile path and runtime requirements). Keep the whole run directory when handing an instance to a coding-agent rollout. The rollout should receive `source/`, `task`, `evaluator/`, and `Dockerfile`; hold `gold/` and `precheck/` back from the solver.

The generated Dockerfile uses `python:3.11-slim`, copies the base repository and the self-contained evaluator, and runs the evaluator on the unchanged base. Build and run from the run directory:

```powershell
docker build -t rf-pilot -f Dockerfile .
docker run --rm rf-pilot
```

The base container is expected to exit with status 1 because no-op must fail. If Docker or its daemon is unavailable, the run records `not_available` with the reason in `docker.json`; host prechecks and export still run. The pilot assumes trusted Python source and standard-library tests, a simple two-argument function, direct imports, and fixed functional examples for USD/EUR. Broader API shapes, dependency installation, stronger behavior checks, and more migration families are future work.
