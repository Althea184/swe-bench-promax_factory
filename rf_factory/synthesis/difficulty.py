"""Objective measurements, without an uncalibrated difficulty label."""
from pathlib import PurePosixPath


def measure(spec, patch):
    callers = spec.metadata["call_counts"]
    return {"affected_file_count": len(patch.changes),
            "caller_module_count": len(callers),
            "dependency_depth": spec.metadata["dependency_depth"],
            "public_api_usages": sum(callers.values()),
            "structural_requirement_count": len(spec.structural_requirements),
            "behavioral_invariant_count": len(spec.behavioral_invariants),
            "cross_module_scope": len({"/".join(PurePosixPath(path).parts[:-1]) for path in spec.scope}),
            "gold_added_lines": sum(change.added_lines for change in patch.changes),
            "gold_deleted_lines": sum(change.deleted_lines for change in patch.changes)}
