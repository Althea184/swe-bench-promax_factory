# HTTPX Historical + Synthetic Compound

This command produces one SWE-Bench ProMax instance from a clean HTTPX Git base. Two consecutive developer commits relocate header and multipart helpers. The factory then rediscovers eligible two-argument helpers in the relocated modules and migrates their local production calls to named arguments. It records the original developer patches and generated changes separately. Gold is one Git diff from the common base to the final workspace.

## Source and task

- Repository: `https://github.com/encode/httpx`
- Base: `66225539796d819b5114ead2fd5e4d61865ab708`
- Historical A: `83a85189c79708b732d04e7b906e388fb0fbde94` moves header normalization helpers from `_utils.py` to `_models.py`.
- Historical B: `6212e8fa3bf90154b891c7721f6d29737caac861` moves multipart formatting helpers from `_utils.py` to `_multipart.py`.
- Synthetic C: require `encoding` by name when calling the relocated header key helper in `_models.py`.
- Synthetic D: require `value` by name when calling the relocated form parameter helper in `_multipart.py`.

The dependency graph records A → B for verified first-parent replay, A → C because the destination helper must exist, and B → D for the same reason. C and D are independent branches after their corresponding relocation. The four tasks change three production files. File count was not inflated to meet a benchmark target.

The solver-facing problem statement is:

> Move HTTP header normalization and multipart form formatting out of the shared utility module into their owning modules. Preserve existing behavior and update production callers. Once relocated, require the header key normalizer's encoding and the multipart form parameter formatter's value to be supplied by name at every production call site.

## Reproduce in PowerShell

Use a complete local HTTPX clone containing both commits. The base image is built once from `docker/httpx-history.Dockerfile`:

```powershell
git clone https://github.com/encode/httpx .rf-factory-work/history-full/httpx
docker build -f docker/httpx-history.Dockerfile -t rf-httpx-history-base:py311 .
python -m rf_factory run-hybrid-compound `
  --commits 83a85189c79708b732d04e7b906e388fb0fbde94 6212e8fa3bf90154b891c7721f6d29737caac861 `
  --repo-url https://github.com/encode/httpx `
  --local-path .rf-factory-work/history-full/httpx `
  --base-image rf-httpx-history-base:py311 `
  --offline-build `
  --test-command 'python -m pytest -q tests/models/test_headers.py tests/client/test_headers.py tests/test_multipart.py' `
  --output runs-hybrid-httpx-complex
```

The output directory must be new. Inspect its single instance-ID subdirectory for `swe-bench-promax.json`, `gold.patch`, `test.patch`, `eval.json`, `eval.sh`, `composition.json`, `subtasks.json`, `relations.json`, `patch_provenance.json`, `difficulty.json`, and `validation.json`. `prepare-rollout` exports only the clean base repository, problem statement, and environment. The hidden tests, Gold patch, and provenance remain controller-side.

The selected upstream regression suite has 68 tests. The factory requires Baseline and Gold to pass, No-op to fail, every proper prefix Partial to fail the full evaluation while passing its own checks, and Gold to apply cleanly on Base. The structural tests accept either the historical developer's renamed helper or a name-preserving relocation, provided the old definition is gone and production callers use the relocated helper. This does not prove full semantic equivalence for every possible input. No Agent difficulty label is assigned without repeated independent rollouts.
