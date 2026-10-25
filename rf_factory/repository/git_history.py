"""Resolve historical Git lineage and commit provenance."""
import re
from pathlib import Path
from ..core.common import FactoryError
from ..core.process import command, require


def resolve_base(clone, record):
    merge = record["merge_commit"]
    parents = require(command(["git", "rev-list", "--parents", "-n", "1", merge], cwd=clone),
                      "Merge commit inspection").split()
    if not parents or parents[0] != merge or len(parents) not in {2, 3}:
        raise FactoryError("Merge commit has an unsupported parent shape")
    if len(parents) == 3:
        return parents[1], "merge_commit"
    if record["head_sha"] != merge:
        return parents[1], "squash_merge"
    commits = record["pr_commits"]
    if len(commits) == 1 and commits[0] == merge:
        return parents[1], "single_commit"
    if commits and commits[-1] == merge:
        for previous, current in zip(commits, commits[1:]):
            lineage = require(command(["git", "rev-list", "--parents", "-n", "1", current],
                                      cwd=clone), "Rebase lineage inspection").split()
            if len(lineage) != 2 or lineage[1] != previous:
                raise FactoryError("Rebased PR commits are not one verifiable first-parent chain")
        first = require(command(["git", "rev-list", "--parents", "-n", "1", commits[0]],
                                cwd=clone), "Rebase base inspection").split()
        if len(first) != 2:
            raise FactoryError("Rebase first commit is ambiguous")
        return first[1], "rebase_fast_forward"
    raise FactoryError("Cannot prove the merged PR base from its commit lineage")





def commit_record(clone: Path, repo_url: str, sha: str):
    """Build provenance from a real Git object when the caller supplies commits."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise FactoryError("Historical compound commits require full SHA values")
    parents = require(command(["git", "rev-list", "--parents", "-n", "1", sha], cwd=clone),
                      "Historical commit lookup").split()
    if len(parents) != 2 or parents[0].lower() != sha.lower():
        raise FactoryError("Commit must be a single-parent historical revision")
    title = require(command(["git", "show", "-s", "--format=%s", sha], cwd=clone),
                    "Historical commit title")
    body = require(command(["git", "show", "-s", "--format=%b", sha], cwd=clone),
                   "Historical commit body")
    files = require(command(["git", "diff", "--name-only", parents[1], sha], cwd=clone),
                    "Historical commit scope").splitlines()
    owner, name = repo_url.rstrip("/").removesuffix(".git").split("/")[-2:]
    number = re.search(r"\(#(\d+)\)\s*$", title)
    return {"repo_url": repo_url, "owner": owner, "name": name,
            "number": int(number.group(1)) if number else None,
            "url": f"{repo_url}/commit/{sha}", "title": title,
            "description": body, "merge_commit": sha.lower(), "head_sha": sha.lower(),
            "pr_commits": [sha.lower()], "pr_files": files,
            "metadata_source": "git_commit", "linked_issue": None}


