"""Turn an evidence-backed historical change into a TaskSpec and Gold."""
from ...core.common import FactoryError, digest
from ...core.models import RefactoringCandidate, RefactoringTaskSpec
from ...core.process import command, require
from ...discovery.historical.analysis import classify, structural_evidence
from ...discovery.synthetic.usage_analysis import module_name
from ...repository.git_history import resolve_base
from ...validation.historical_acceptance import choose_strategy


def discover_history(record, clone):
    base, merge_method = resolve_base(clone, record)
    target = record["merge_commit"]
    changed = require(command(["git", "diff", "--name-only", base, target], cwd=clone),
                      "Developer changed-file inspection").splitlines()
    if set(changed) != set(record["pr_files"]):
        raise FactoryError("Merged commit files differ from the PR file list; developer scope is ambiguous")
    raw = require(command(["git", "diff", "--binary", "--no-ext-diff", base, target], cwd=clone),
                  "Developer patch extraction") + "\n"
    if not raw.strip():
        raise FactoryError("Developer patch is empty")
    evidence = structural_evidence(clone, base, target, changed)
    classification, reason = classify(record, evidence)
    if reason:
        raise FactoryError(reason)
    strategy = choose_strategy(record, classification, evidence)
    base_files = set(require(command(["git", "ls-tree", "-r", "--name-only", base], cwd=clone),
                             "Base tree inspection").splitlines())
    modules = tuple(sorted({module_name(path, base_files) for path in evidence["production_paths"]
                            if path.endswith(".py")}))
    identity = digest(f"{record['url']}:{base}:{target}:{digest(raw.encode())}".encode())[:20]
    source_refs = (({"kind": "commit", "sha": target, "url": record["url"]},) if
        record.get("metadata_source") == "git_commit" else
        ({"kind": "pull_request", "url": record["url"], "number": record["number"]},
         {"kind": "commit", "sha": target}))
    candidate = RefactoringCandidate(identity, record["repo_url"], base, "historical",
        source_refs,
        classification["types"], classification["symbols"], modules,
        strategy["goal"], strategy["invariants"], strategy["compatibility"],
        "developer_patch", evidence={**evidence, "workflow": classification["workflow"],
                                    "intent_excerpt": record["description"]},
        metadata={"target_commit": target, "merge_method": merge_method,
                  "acceptance_strategy": strategy["id"],
                  "raw_patch_sha256": digest(raw.encode())})
    task = RefactoringTaskSpec(candidate, candidate.goal, tuple(changed),
        strategy["requirements"],
        candidate.behavioral_invariants, candidate.compatibility_requirements,
        strategy["id"], {"source": ("commit message and structural evidence" if
            record.get("metadata_source") == "git_commit" else
            "PR title and description plus structural evidence"),
                         "acceptance_coverage": strategy["coverage"],
                         "raw_patch_sha256": digest(raw.encode())})
    return task, raw, {"base_commit": base, "target_commit": target,
                       "merge_method": merge_method, "changed_files": changed,
                       "classification": classification, "evidence": evidence}


