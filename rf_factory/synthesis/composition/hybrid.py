"""Build the existing four-subtask historical and synthetic dependency graph."""
from ...core.models import CompoundTaskSpec


def compose_hybrid(tasks, commits, synthetic_steps):
    ids = [task.candidate.candidate_id for task in tasks]
    edges = ((ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[3]))
    scope = tuple(sorted(set().union(*(set(task.scope) for task in tasks))))
    goal = ("Move HTTP header normalization and multipart form formatting out of the "
            "shared utility module into their owning modules. Preserve existing behavior "
            "and update production callers. Once relocated, require the header key "
            "normalizer's encoding and the multipart form parameter formatter's value "
            "to be supplied by name at every production call site.")
    composition = CompoundTaskSpec(tuple(tasks), edges, goal, scope,
        {"source_types": ["historical", "historical", "synthetic", "synthetic"],
         "relation": "relocation enables local interface evolution",
         "historical_commits": commits, "synthetic_steps": synthetic_steps})
    return composition, edges, scope, goal
