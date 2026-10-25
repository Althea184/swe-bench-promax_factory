"""Explainable minimum-quality filtering before task specification."""


def filter_and_rank(candidates, config):
    selected, rejected = [], []
    for candidate in sorted(candidates, key=lambda item: (item.path, item.start_line)):
        metrics = candidate.metrics
        reasons = []
        if candidate.strategy not in {"api-migration-v2", "module-responsibility-v1"}:
            reasons.append("unsupported strategy")
        if metrics.get("caller_modules", 0) < config.min_caller_modules:
            reasons.append("too few caller modules")
        if metrics.get("potential_files", 0) < config.min_changed_files:
            reasons.append("too few changed files")
        if metrics.get("behavioral_invariants", 0) == 0:
            reasons.append("no behavioral constraints")
        if not metrics.get("changes_calling_convention"):
            reasons.append("mechanical rename only")
        if reasons:
            rejected.append({"candidate_id": candidate.candidate_id, "stage": "candidate_filtering", "reasons": reasons})
        elif len(selected) < config.max_candidates:
            selected.append(candidate)
        else:
            rejected.append({"candidate_id": candidate.candidate_id, "stage": "candidate_filtering",
                             "reasons": ["max_candidates limit"]})
    return selected, rejected
