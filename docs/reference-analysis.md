# Engineering reference: swe-series-factory

The neighboring `swe-series-factory` was inspected read-only. Its families use explicit CLI entry points, repository acquisition, stage-specific modules, portable evaluator/verifier artifacts, intermediate JSON evidence, final packaging, and fixture-backed tests. `nl2repo_factory` in particular separates source, specification, oracle, verifier, and packaging. Those boundaries informed this repository's organization.

The refactoring factory has its own domain flow and data contracts. It does not import or copy the neighboring benchmark's task algorithms, Docker environment construction, agent roles, or repair loop. There is one `Pipeline`, one `Instance` model, and one implemented API migration strategy. Future refactoring families can add discovery, spec-building, gold-generation, and type-specific oracle checks while using the same problem, evaluation, instance, CLI, and artifact stages.

The current implementation deliberately avoids a plugin framework and empty strategy classes. The research boundaries are visible as `Candidate`, `TaskSpec`, `GoldPatch`, `ProblemStatement`, `Oracle`, `Evaluation`, and `Instance` records. Runs retain these stages separately for debugging and ablation.
