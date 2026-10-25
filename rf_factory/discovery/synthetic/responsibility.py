"""Discover duplicate module responsibilities."""
import ast
import builtins
from ...core.common import digest
from ...core.models import Candidate


def module_path(module):
    return module.replace(".", "/") + ".py"


def local_function(root, path, symbol, class_name="", allowed_globals=()):
    with (root / path).open("r", encoding="utf-8", newline="") as stream:
        source = stream.read()
    tree = ast.parse(source)
    owner = tree.body
    if class_name:
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if len(classes) != 1:
            raise ValueError("Source class missing or ambiguous")
        owner = classes[0].body
    matches = [n for n in owner if isinstance(n, ast.FunctionDef) and n.name == symbol]
    if len(matches) != 1 or matches[0].decorator_list:
        raise ValueError("Source function missing, ambiguous, or decorated")
    fn = matches[0]
    if fn.args.vararg or (fn.args.kwarg and not class_name) or fn.args.kwonlyargs or fn.args.posonlyargs:
        raise ValueError("Unsupported source function signature")
    params = {arg.arg for arg in fn.args.args}
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)
    assigned = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    loaded = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if loaded - params - assigned - set(dir(builtins)) - set(allowed_globals):
        raise ValueError("Function depends on source-module globals")
    return source, tree, fn


class ResponsibilityDiscovery:
    def discover(self, repo, config):
        candidates, diagnostics = [], []
        for recipe in config.responsibility_migrations:
            paths = [module_path(module) for module in recipe.source_modules]
            target = module_path(recipe.target_module)
            try:
                if target not in repo.files or any(path not in repo.files for path in paths):
                    raise ValueError("Configured module missing")
                target_tree = ast.parse((repo.snapshot / target).read_text(encoding="utf-8"))
                if any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == recipe.symbol for n in target_tree.body):
                    raise ValueError("Target symbol already exists")
                allowed = [item.split(" import ", 1)[1].split(" as ")[-1] for item in recipe.target_imports]
                definitions = [local_function(repo.snapshot, path, recipe.symbol, recipe.class_name, allowed) for path in paths]
                shape = ast.dump(definitions[0][2], include_attributes=False)
                if any(ast.dump(fn, include_attributes=False) != shape for _, _, fn in definitions[1:]):
                    raise ValueError("Source implementations are not equivalent")
                counts = {}
                for path, (_, tree, fn) in zip(paths, definitions):
                    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                             and ((isinstance(n.func, ast.Name) and n.func.id == recipe.symbol)
                                  or (recipe.class_name and isinstance(n.func, ast.Attribute) and n.func.attr == recipe.symbol))]
                    if recipe.class_name:
                        calls = [fn]
                    if not calls:
                        raise ValueError("Source module does not call its local implementation")
                    counts[path] = len(calls)
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                diagnostics.append({"path": paths[0], "error": str(exc)})
                continue
            fn = definitions[0][2]
            identity = digest(f"{repo.content_sha256}:{paths}:{target}:{recipe.symbol}:responsibility-v1".encode())[:20]
            metrics = {"caller_modules": len(paths), "potential_files": len(paths) + 1,
                       "behavioral_invariants": len(recipe.behavioral_invariants),
                       "changes_calling_convention": True, "local_call_counts": counts}
            candidates.append(Candidate(identity, paths[0], recipe.symbol, fn.lineno, fn.end_lineno,
                                        repo.files[paths[0]], "Duplicate responsibility in caller modules",
                                        "module-responsibility-v1", metrics))
        return candidates, diagnostics


