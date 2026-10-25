"""Small source-neutral adapter and conservative relationship checks."""
from __future__ import annotations

from ..core.common import digest
from ..core.models import RefactoringCandidate, RefactoringTaskSpec
from ..discovery.synthetic.usage_analysis import module_name


def synthetic_candidate(spec, *, repository, base_commit, files):
    """Project a legacy API TaskSpec without changing its existing schema."""
    modules = tuple(sorted({module_name(path, files) for path in spec.scope if path.endswith(".py")}))
    candidate = RefactoringCandidate(
        candidate_id=spec.candidate.candidate_id, repository=repository,
        base_commit=base_commit, source_type="synthetic", source_refs=(),
        refactoring_types=(spec.refactoring_type,),
        affected_symbols=(spec.candidate.symbol,), affected_modules=modules,
        goal=spec.goal, behavioral_invariants=spec.behavioral_invariants,
        compatibility_requirements=spec.compatibility_requirements,
        gold_source="generated_patch", evidence={"legacy_candidate_id": spec.candidate.candidate_id},
        metadata={"strategy": spec.candidate.strategy,
                  "affected_callers": sum(spec.metadata.get("call_counts", {}).values())})
    return RefactoringTaskSpec(candidate, spec.goal, spec.scope, spec.structural_requirements,
                               spec.behavioral_invariants, spec.compatibility_requirements,
                               spec.metadata.get("strategy", "api-migration-v1"),
                               {"legacy_task_spec": True})


def relationship(left: RefactoringCandidate, right: RefactoringCandidate):
    """Return explicit evidence or a refusal; file proximity alone is insufficient."""
    if left.repository != right.repository:
        return None, "different repositories"
    consecutive = (left.source_type == right.source_type == "historical" and
                   left.metadata.get("target_commit") == right.base_commit)
    if left.base_commit != right.base_commit and not consecutive:
        return None, "no shared reproducible base or consecutive historical commits"
    shared_symbols = sorted(set(left.affected_symbols) & set(right.affected_symbols))
    direct = sorted(set(left.dependencies) & set(right.affected_symbols) |
                    set(right.dependencies) & set(left.affected_symbols))
    workflow = left.evidence.get("workflow")
    shared_workflow = workflow if workflow and workflow == right.evidence.get("workflow") else None
    if not (shared_symbols or direct or shared_workflow):
        return None, "no symbol, dependency, or evidenced workflow relationship"
    return {"kind": "consecutive_history" if consecutive else "shared_base",
            "shared_symbols": shared_symbols, "direct_dependencies": direct,
            "shared_workflow": shared_workflow,
            "base_commit": left.base_commit}, None


def difficulty_metadata(tasks, patch, changed_files, relation=None):
    types = set().union(*(set(task.candidate.refactoring_types) for task in tasks))
    modules = set().union(*(set(task.candidate.affected_modules) for task in tasks))
    return {"subtask_count": len(tasks), "refactoring_type_count": len(types),
            "changed_file_count": len(set(changed_files)), "affected_module_count": len(modules),
            "patch_line_count": sum(line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                                    for line in patch.splitlines()),
            "affected_caller_count": (sum(task.candidate.metadata["affected_callers"] for task in tasks)
                                      if all(isinstance(task.candidate.metadata.get("affected_callers"), int)
                                             for task in tasks) else None),
            "cross_module_dependency_count": sum(len(task.candidate.dependencies) for task in tasks),
            "operation_dependencies": (relation or {}).get("direct_dependencies", []),
            "sources": [task.candidate.source_type for task in tasks],
            "fingerprint": digest(patch.encode())[:16]}
