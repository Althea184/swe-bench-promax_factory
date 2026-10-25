"""Discover API migration recipes from real repository snapshots."""
import ast
from pathlib import Path
from ...core.config import MigrationRecipe
from .usage_analysis import UsageIndex, module_name


def discover_recipes(repo, *, include_rejections=False):
    """Infer simple keyword-only migrations from definitions and their static calls."""
    recipes, diagnostics, rejections = [], [], []
    files = [path for path in repo.files if path.endswith(".py") and not
             any(part in {"tests", "test", "fixtures"} for part in Path(path).parts)]
    for path in sorted(files):
        try:
            tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            diagnostics.append({"path": path, "error": str(exc)})
            continue
        module = module_name(path, repo.files)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            identity = {"path": path, "module": module, "symbol": fn.name, "line": fn.lineno}
            if fn.name.startswith("_"):
                if include_rejections:
                    rejections.append({**identity, "reason": "private top-level function"})
                continue
            args = fn.args
            if (fn.decorator_list or args.posonlyargs or len(args.args) != 2 or
                    len(args.defaults) > 1 or args.kwonlyargs or args.vararg or args.kwarg):
                if include_rejections:
                    rejections.append({**identity, "reason": "unsupported signature or decorators"})
                continue
            try:
                index = UsageIndex(repo.snapshot, files, module, fn.name)
                sites = index.call_sites()
                if not sites:
                    if include_rejections:
                        rejections.append({**identity, "reason": "no statically resolved production callers"})
                    continue
                if any(len(call.args) != 2 or call.keywords
                       for calls in sites.values() for call in calls):
                    if include_rejections:
                        rejections.append({**identity, "reason": "unsupported production call shape"})
                    continue
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                diagnostics.append({"path": path, "symbol": fn.name, "error": str(exc)})
                if include_rejections:
                    rejections.append({**identity, "reason": f"call graph analysis failed: {exc}"})
                continue
            parameter = args.args[1].arg
            recipes.append((len(sites), sum(map(len, sites.values())),
                            MigrationRecipe(module, fn.name, fn.name, parameter,
                                f"Make the {fn.name} API require an explicit named {parameter} argument and migrate its callers.",
                                ("Preserve the existing behavior and validation of this API.",
                                 "Keep the original project regression suite passing."),
                                ("All production callers must use the named argument.",),
                                (), auto_discovered=True)))
    recipes.sort(key=lambda item: (-item[0], -item[1], item[2].module, item[2].symbol))
    result = ([item[2] for item in recipes], diagnostics)
    return (*result, rejections) if include_rejections else result


