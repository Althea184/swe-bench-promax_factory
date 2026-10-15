"""Deterministic AST heuristic; never imports the analyzed source."""
import ast
import tokenize
from .common import digest
from .models import Candidate, Repository
from .config import Config


class LongFunctionDiscovery:
    def discover(self, repo: Repository, config: Config) -> tuple[list[Candidate], list[dict]]:
        candidates, diagnostics = [], []
        for name in sorted(repo.files):
            if not name.endswith(".py") or name.startswith(config.test_dir + "/") or name.rsplit("/", 1)[-1].startswith("test"):
                continue
            try:
                with tokenize.open(repo.snapshot / name) as stream:
                    tree = ast.parse(stream.read())
            except (SyntaxError, UnicodeError, LookupError) as exc:
                diagnostics.append({"path": name, "error": str(exc)})
                continue
            # Only module-level synchronous functions in MVP: explicit, unambiguous targets.
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef) or node.decorator_list:
                    continue
                length = node.end_lineno - node.lineno + 1
                if length < config.min_function_lines or len(node.body) < 3:
                    continue
                identity = digest(f"{repo.content_sha256}:{name}:{node.name}:{node.lineno}".encode())[:20]
                candidates.append(Candidate(identity, name, node.name, node.lineno, node.end_lineno,
                                            repo.files[name], f"Function spans {length} lines with {len(node.body)} top-level statements"))
        return candidates[:config.max_candidates], diagnostics
