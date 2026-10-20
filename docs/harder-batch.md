# Harder API migration batch

`examples/harder-api-migration.toml` supplies five migration recipes for the coherent `commerce_repo` fixture. The recipe catalog is kept outside the repository snapshot, so a solver receives no target mapping or oracle cases. Discovery still checks that each configured API and its call graph exist. It is **not yet automatic opportunity mining** from arbitrary repositories.

The fixture has public entrypoints, a checkout workflow, service wrappers, reports, a scheduled job, and a partner adapter. Usages include direct imports, module-qualified calls, and an import through a public re-export. Each task changes one API's second positional parameter to keyword-only and propagates the calling convention while preserving domain behavior. The API symbol stays the same, so these tasks are not rename exercises. The five domains are loyalty discount, stock allocation, dispatch notice, shipping quote, and tax estimate.

```powershell
python -m rf_factory run --config examples/harder-api-migration.toml --output runs
python -m rf_factory prepare-rollout --run runs/rf-6842b930b7d5f16253d5e9ff --instance rf-6842b930b7d5f16253d5e9ff --output <separate-solver-directory>
```

`prepare-rollout` writes only `repository/`, `task.md`, and `environment.json` into a fresh directory outside the controller run. Give that directory to a solver. Keep `config.json`, TaskSpec, oracle, verifier spec, gold, and precheck in the controller run.

The run records objective `difficulty_metadata`: changed files, caller modules, import-graph depth, target API usages, requirement counts, package scope, and gold line changes. Depth is the longest shortest static import path from a production module to the API. These measures have no calibrated easy/medium/hard label.

Quality gates require passing base tests, a valid candidate and TaskSpec, passing problem quality checks, an applicable gold patch, gold PASS, and no-op FAIL on at least two independent checks. Verifier cases cover supported values, defaults where applicable, invalid values, exception types/messages, and rejection of the former positional calling convention. The static checker does not compare patch bytes. The five generated tasks are useful for rollout trials, but their actual difficulty and benchmark value require coding-agent runs and review of alternative valid solutions.
