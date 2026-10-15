import ast
import tokenize
from .common import digest
from .models import Validation
from .repository import manifest


class TaskValidator:
    def validate(self, repo, task, baseline, config):
        candidate = task.candidate
        path = repo.snapshot / candidate.path
        exists = path.is_file() and path.resolve().is_relative_to(repo.snapshot.resolve())
        target = False
        if exists:
            try:
                with tokenize.open(path) as stream:
                    tree = ast.parse(stream.read())
                target = sum(isinstance(n, ast.FunctionDef) and n.name == candidate.symbol
                             and n.lineno == candidate.start_line and n.end_lineno == candidate.end_line
                             for n in tree.body) == 1
            except (SyntaxError, UnicodeError, LookupError):
                pass
        checks = {
            "repository_unchanged": manifest(repo.snapshot) == repo.files,
            "candidate_exists": exists,
            "candidate_hash_matches": exists and digest(path.read_bytes()) == candidate.source_sha256,
            "target_unambiguous": target,
            "required_fields": bool(task.problem_statement.strip() and task.requirements and task.template_sha256),
            "baseline_tests": baseline.get("passed", False) or (not config.require_tests and baseline.get("status") == "not_available"),
        }
        result = Validation(all(checks.values()), checks, baseline)
        if baseline.get("status") == "not_available":
            result.limitations.append("No tests available; tests explicitly optional in configuration.")
        return result
