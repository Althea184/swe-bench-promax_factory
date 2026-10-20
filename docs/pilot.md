# API migration fixture walkthrough

Run `python -m rf_factory demo --output runs` from the project root. The resulting directory is `runs/<instance-id>/`. This is the one current pipeline, exercised with a small deterministic case.

The original repository has `money.py` defining `render_amount(amount, currency)`. `invoice.py` and `quote.py` directly import and call it; `tests/test_money.py` covers USD, EUR, and a negative amount. Discovery identifies the definition with two caller modules as an opportunity, then filtering selects it.

The structured `TaskSpec` records type `api_migration`, goal, the three-file scope, current/target APIs, required keyword-only currency, caller counts, behavioral invariants, and compatibility. It is internal evidence. Gold generation independently changes the definition to `format_amount(amount, *, currency)` and migrates the three direct calls. The patch is checked against source hashes before application.

The solver-facing statement is:

> Use clearer naming for the public amount-formatting API and migrate dependent callers. Preserve formatted output and validation errors. Keep existing regression tests passing. Dependent callers must use the replacement API and supply currency explicitly by name.

It names the functionality, migration goal, behavior, and required call semantics. It does not reveal the new symbol, files, call-site inventory, line numbers, or edit sequence. Thus it is specific enough for the oracle's public contract without providing the gold transformation.

The oracle captures structural conditions for the migrated API and callers, source hashes to reject no-op, the regression suite, and USD/EUR functional examples. The portable verifier runs those checks; the evaluator aggregates them. In the clean local run, gold passed every check and unchanged source failed the migration checks while still passing regression tests. Different source bytes can pass; this oracle does not compare a solver patch to gold.

The final `rf.instance.v3` row includes the base repository, TaskSpec, solver-facing statement, oracle, gold patch manifest, verifier command, gold/no-op precheck, and environment. `instances.jsonl` is a controller-side dataset export. Give a solver only the base repository and `problem_statement.text`; withhold the internal spec, oracle, gold, and precheck. This fixture does not establish that arbitrary real repository API migrations can be synthesized or verified.
