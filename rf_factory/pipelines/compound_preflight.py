"""Report synthetic compound eligibility before Docker work."""
from dataclasses import asdict
from ..synthesis.candidate_pool import synthetic_candidate
from ..synthesis.composition.synthetic import candidates_for
from ..discovery.synthetic.usage_analysis import UsageIndex


def discovery_preflight(repo, recipes, test_argv, discovery_diagnostics, discovery_rejections,
                        repository=None, base_commit=None):
    """Report source-backed candidates and relations without building Docker."""
    pairs, rejected, accepted = candidates_for(repo, recipes, test_argv, include_candidates=True)
    candidates = []
    for recipe, spec in accepted:
        index = UsageIndex(repo.snapshot, repo.files, recipe.module, recipe.symbol)
        callers = [{"path": path, "module": index.module_for_path(path),
                    "calls": [{"line": node.lineno, "column": node.col_offset,
                               "end_line": node.end_lineno} for node in nodes]}
                   for path, nodes in sorted(index.call_sites().items())]
        candidates.append({"candidate_id": spec.candidate.candidate_id,
                           "module": recipe.module, "path": spec.candidate.path,
                           "symbol": recipe.symbol, "parameter": recipe.parameter,
                           "line": spec.candidate.start_line, "callers": callers})
    unified = ([asdict(synthetic_candidate(spec, repository=repository,
                     base_commit=base_commit, files=repo.files).candidate)
                for _, spec in accepted] if repository and base_commit else [])
    return {"status": "eligible_pairs_found" if pairs else "no_eligible_pair",
            "candidate_count": len(candidates), "pair_count": len(pairs),
            "candidates": candidates, "unified_candidates": unified,
            "pairs": [{"subtasks": [{"module": item.module, "symbol": item.symbol}
                                    for item in pair[:2]], "relation": pair[4]}
                      for pair in pairs],
            "rejections": [*discovery_rejections, *rejected],
            "diagnostics": discovery_diagnostics}


