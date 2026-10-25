"""Controller-side planning for consecutive historical refactorings."""
from __future__ import annotations

import shutil
import os
import stat
from pathlib import Path

from ..candidate_pool import difficulty_metadata, relationship
from ...core.common import FactoryError
from ...core.process import command, require


def compose_consecutive(first, second, source: Path, workspace: Path):
    """Apply two recorded revisions in isolation and diff the final tree once."""
    relation, reason = relationship(first.candidate, second.candidate)
    if reason:
        raise FactoryError(reason)
    if relation["kind"] != "consecutive_history":
        raise FactoryError("Historical composition requires consecutive recorded commits")
    if workspace.exists():
        raise FactoryError("Composition workspace already exists")
    require(command(["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks",
                     "--", str(source.resolve()), str(workspace)]), "Composition clone")
    try:
        require(command(["git", "config", "core.autocrlf", "false"], cwd=workspace),
                "Composition newline configuration")
        base = first.candidate.base_commit
        targets = [first.candidate.metadata["target_commit"],
                   second.candidate.metadata["target_commit"]]
        records = []
        for number, target in enumerate([base, *targets]):
            require(command(["git", "checkout", "--detach", target], cwd=workspace),
                    "Composition revision checkout")
            if require(command(["git", "status", "--porcelain"], cwd=workspace),
                       "Composition worktree check"):
                raise FactoryError("Composition revision has a dirty worktree")
            if number:
                previous = [base, *targets][number - 1]
                changed = require(command(["git", "diff", "--name-only", previous, target],
                                          cwd=workspace), "Composition step diff").splitlines()
                if not changed:
                    raise FactoryError("Historical composition contains an empty step")
                records.append({"step": number - 1, "from": previous, "to": target,
                                "changed_files": changed})
        patch = require(command(["git", "diff", "--binary", "--no-ext-diff", base,
                                 targets[-1]], cwd=workspace), "Final compound diff") + "\n"
        if not patch.strip():
            raise FactoryError("Final historical compound patch is empty")
        files = require(command(["git", "diff", "--name-only", base, targets[-1]],
                                cwd=workspace), "Final changed-file inspection").splitlines()
        return {"relation": relation, "steps": records, "base_commit": base,
                "target_commit": targets[-1], "gold_patch": patch,
                "difficulty": difficulty_metadata((first, second), patch, files, relation),
                "exportable": False,
                "export_blocker": "Independent subtask acceptance and Partial controls are not yet established"}
    finally:
        if workspace.exists():
            if not workspace.resolve().is_relative_to(workspace.parent.resolve()):
                raise FactoryError("Unsafe composition cleanup path")
            def writable_remove(operation, target, _error):
                os.chmod(target, stat.S_IWRITE)
                operation(target)
            shutil.rmtree(workspace, onerror=writable_remove)


def historical_relation(first, second, first_discovery, second_discovery):
    """Require version adjacency and a structural relationship, separately."""
    a, b = first.candidate, second.candidate
    if a.repository != b.repository:
        raise FactoryError("Historical subtasks come from different repositories")
    if a.metadata["target_commit"] != b.base_commit:
        raise FactoryError("Historical revisions are not consecutive; intermediate commits are excluded")
    left = first_discovery["classification"]["operations"]
    right = second_discovery["classification"]["operations"]
    relocations = ([op for op in left if op["kind"] == "relocation"],
                   [op for op in right if op["kind"] == "relocation"])
    shared_sources = sorted({op["from_module"] for op in relocations[0]} &
                            {op["from_module"] for op in relocations[1]})
    destinations = sorted({op["to_module"] for group in relocations for op in group})
    if (len(relocations[0]) == len(left) and len(relocations[1]) == len(right)
            and shared_sources and len(destinations) >= 2
            and all(op.get("callers") for group in relocations for op in group)):
        return {"kind": "shared_architectural_responsibility",
                "evidence": "successive relocation of called helpers from one utility module to their owning modules",
                "shared_source_modules": shared_sources, "destination_modules": destinations,
                "historical_order": [a.metadata["target_commit"], b.metadata["target_commit"]],
                "patch_compatibility": "consecutive_first_parent"}
    symbols_a = set(a.affected_symbols)
    symbols_b = set(b.affected_symbols)
    overlap = sorted(symbols_a & symbols_b)
    shared_modules = sorted(set(a.affected_modules) & set(b.affected_modules))
    if overlap and shared_modules:
        return {"kind": "shared_symbols", "evidence": "both changes affect named production symbols",
                "shared_symbols": overlap, "shared_modules": shared_modules, "historical_order":
                [a.metadata["target_commit"], b.metadata["target_commit"]],
                "patch_compatibility": "consecutive_first_parent"}
    raise FactoryError("No evidenced call or shared architectural responsibility links the two refactorings")


