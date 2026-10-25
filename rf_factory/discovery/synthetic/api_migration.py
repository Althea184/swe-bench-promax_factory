"""Discover supported configured API migrations."""
import ast
from ...core.common import digest
from ...core.models import Candidate
from .usage_analysis import UsageIndex, module_path


class ApiMigrationDiscovery:
    """Find configured APIs and supported static usages; leave quality cuts to filtering."""
    def discover(self, repo, config):
        candidates, diagnostics = [], []
        for recipe in config.api_migrations:
            try:
                path = module_path(recipe.module, repo.files)
            except ValueError as exc:
                diagnostics.append({"module": recipe.module, "error": str(exc)})
                continue
            try:
                tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
                functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == recipe.symbol]
                if len(functions) != 1:
                    raise ValueError("configured API definition missing or ambiguous")
                fn = functions[0]
                if (fn.decorator_list or fn.args.posonlyargs or len(fn.args.args) != 2
                        or fn.args.args[1].arg != recipe.parameter or len(fn.args.defaults) > 1
                        or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg):
                    raise ValueError("unsupported API signature")
                index = UsageIndex(repo.snapshot, repo.files, recipe.module, recipe.symbol)
                calls = index.call_sites(include_local=config.include_local_calls)
                if recipe.symbol != recipe.target_symbol and any(
                        name != recipe.module and index.exports(name, recipe.symbol)
                        and any(isinstance(node, ast.ImportFrom) and index.import_module(tree_module, node) == name
                                and any(alias.name == recipe.symbol for alias in node.names)
                                for tree_module, tree in index.trees.items() for node in tree.body)
                        for name in index.trees):
                    raise ValueError("renamed API through re-export is unsupported")
                if any(len(call.args) != 2 or call.keywords for nodes in calls.values() for call in nodes):
                    raise ValueError("unsupported target call shape")
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                diagnostics.append({"path": path, "error": str(exc)})
                continue
            identity = digest(f"{repo.content_sha256}:{path}:{fn.lineno}:{recipe.symbol}:api-migration-v2".encode())[:20]
            metrics = {"caller_modules": len(calls), "public_api_usages": sum(map(len, calls.values())),
                       "potential_files": len(calls) + 1, "dependency_depth": index.dependency_depth(),
                       "behavioral_invariants": len(recipe.behavioral_invariants),
                       "changes_calling_convention": True}
            candidates.append(Candidate(identity, path, fn.name, fn.lineno, fn.end_lineno, repo.files[path],
                                        f"API used in {len(calls)} production modules", "api-migration-v2", metrics))
        return candidates, diagnostics


