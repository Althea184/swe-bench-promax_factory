# Historical refactoring: eight-PR experiment

The historical route keeps the existing `run-history -> Docker controls -> publish_run`
pipeline. `history_analysis.py` reads the merged Git diff and Python ASTs, records
atomic operations, and assigns macro labels. `history_acceptance.py` selects one
strategy only when it covers every accepted operation. `history_scan.py` reuses
GitHub API responses and repository Git objects while producing reports without
building Docker images. The preflight report distinguishes eligibility from a
verified, published Instance.

## Batch preflight

Input: [`examples/historical-prs.json`](../examples/historical-prs.json). The
actual report is `runs-history-scan-delivery-20261024/`. All base revisions
below came from the merged commit's Git ancestry, not from a PR branch HEAD.

| PR | Base Commit | AST classification | Preflight decision |
| --- | --- | --- | --- |
| [packaging #793](https://github.com/pypa/packaging/pull/793) | `c385b58c4d0e4d66f5c8273ebb155801c92aadf3` | Extraction, Decomposition | Eligible; version renderer behavior strategy |
| [Click #3372](https://github.com/pallets/click/pull/3372) | `25edc1e31360efba4f64c8c3c540bbbcfb7876da` | Extraction, Decomposition, Interface Evolution | Eligible; structural operations strategy |
| [HTTPX #3382](https://github.com/encode/httpx/pull/3382) | `66225539796d819b5114ead2fd5e4d61865ab708` | Relocation | Eligible; structural operations strategy |
| [Requests #5924](https://github.com/psf/requests/pull/5924) | `590350f8d094c216051510ed1dd18fe871b53b72` | Extraction, Decomposition evidence | Rejected: production and test changes cannot yet be separated conservatively |
| [Click #2517](https://github.com/pallets/click/pull/2517) | `a6e0d2936891030f5092c9869c7391104f6721d9` | Unconfirmed | Rejected: PR explicitly also fixes an issue |
| [Requests #6187](https://github.com/psf/requests/pull/6187) | `2ecdb685619340735fa85d253c4779029957246f` | No supported atom | Rejected: dictionary-comprehension cleanup does not match the supported structural operations |
| [Click #2202](https://github.com/pallets/click/pull/2202) | `4a888774ef3c5c80cf3c416d7407e13ad22359f0` | No supported atom | Rejected: helper inlining is not an implemented strategy |
| [Werkzeug #2415](https://github.com/pallets/werkzeug/pull/2415) | `b42d1a4b57b8aba5c38fb8a030848db71004e2c5` | Extraction, Decomposition evidence | Rejected: PR explicitly combines extraction with an issue fix |

The first scan encountered GitHub API rate limiting, and the second encountered
intermittent TLS EOF responses. Those runs were not treated as classification
results. The final report reused cached official API responses and Git objects:
3 eligible, 5 rejected. `candidates.json`, `rejections.json`,
`preflight_reports.json`, and `preflight_summary.json` retain the complete
per-PR evidence and reasons.

## Verified new instances

Both Gold patches are byte-for-byte equal to their corresponding saved
`original-pr.patch` developer diffs. Original regression tests remain intact.

| Instance | Relevant original Baseline | No-op | Gold | Clean Apply | Solver rollout |
| --- | --- | --- | --- | --- | --- |
| `runs-history-click-pr3372-20261024/pallets__click-h_25edc1e-pr3372-d100dd3d/` | 40 passed | structural test fails on both extraction and interface default | 40 original + 1 structural test pass | pass | clean Base, task, environment only |
| `runs-history-httpx-pr3382-v3-20261024/encode__httpx-h_6622553-pr3382-c366c1a2/` | 30 passed | structural test fails on both moved functions | 30 original + 1 structural test pass | pass | clean Base, task, environment only |

Click's task extracts `_guess_type` from `convert_type` and makes the `ty`
parameter optional. HTTPX's task relocates both header-normalization functions
from `httpx._utils` to `httpx._models` and updates their production callers.
The acceptance strategy checks these structures with AST queries; it never
compares a proposed patch with the developer patch. Existing regression suites
check behavior. These tests do not prove full semantic equivalence for arbitrary
alternative implementations.

The HTTPX Docker image includes its declared header-test dependencies.
An initial image lacked Hatchling's `editables`; another lacked `brotli` and
`zstandard`, making unchanged original `Accept-Encoding` tests fail. These
environment attempts were rejected before Gold. The final image and suite
passed without changing HTTPX source or weakening its tests. See
[`docker/httpx-history.Dockerfile`](../docker/httpx-history.Dockerfile).

## Reproduce

Use a fresh `--output` name on each run. A populated `--cache` can be reused
to avoid repeated GitHub API calls; omit `--local-path` to clone from GitHub.

```powershell
python -m rf_factory history-scan --input examples/historical-prs.json --output runs-history-scan-repro --cache .rf-factory-work/history-cache
docker build -t rf-packaging-test-base:py311 -f docker/packaging.Dockerfile docker
docker build -t rf-httpx-history-base:py311 -f docker/httpx-history.Dockerfile docker
python -m rf_factory run-history --pr-url https://github.com/pallets/click/pull/3372 --cache .rf-factory-work/history-cache --base-image rf-packaging-test-base:py311 --offline-build --test-command 'python -m pytest -q tests/test_types.py' --output runs-history-click-repro
python -m rf_factory run-history --pr-url https://github.com/encode/httpx/pull/3382 --cache .rf-factory-work/history-cache --base-image rf-httpx-history-base:py311 --offline-build --test-command 'python -m pytest -q tests/models/test_headers.py tests/client/test_headers.py' --output runs-history-httpx-repro
```

The earlier packaging PR #793 also passed the generalized route again:
26,846 original tests passed in Baseline and Gold, No-op failed its structural
acceptance, and clean Gold application passed. Output:
`runs-history-packaging-generalized-regression-20261024/`.
The existing Synthetic Compound Docker route was rerun at
`peng-weihan/swe-series-factory@661d5b978a30e6d3517ad8232451353bb5e81ce1`:
Baseline and Gold passed; No-op and both incomplete Partial variants failed;
each Partial variant passed its own acceptance. Output:
`runs-compound-historical-generalization-regression-20261024/`.

## Boundaries

The automatic strategy covers definition extraction, relocation, and a
limited positional/default interface change. Decomposition is a macro label
when extraction separates an existing function's responsibility; it has no
independent general verifier yet. Test or documentation edits, combined bug
fixes, helper inlining, broad cross-module API rewrites, and uncertain matches
are rejected or require review. Static AST analysis cannot fully resolve
dynamic imports, reflection, decorators, aliasing, or polymorphic calls.
Public PR history can reveal the developer solution to a networked solver;
each historical Instance records that provenance risk. Historical Compound
and Hybrid composition remain unimplemented until each subtask has independent
acceptance and executable Partial controls.
