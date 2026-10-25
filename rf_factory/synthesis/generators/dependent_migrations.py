"""Find API migrations dependent on historical relocations."""
import ast
from ...core.common import FactoryError
from ...core.config import Config, MigrationRecipe
from ...discovery.synthetic.api_migration import ApiMigrationDiscovery
from ...discovery.synthetic.usage_analysis import module_name
from .api_migration import ApiMigrationSpecBuilder


def _local_migrations(repo, historical_specs):
    """Find supported two-argument helpers created by each historical relocation."""
    proposals, rejected = [], []
    for historical in historical_specs:
        options = []
        for operation in historical["candidate"]["evidence"]["atomic_operations"]:
            if operation["kind"] != "relocation":
                continue
            path, symbol = operation["to_module"], operation["to_symbol"]
            module = module_name(path, repo.files)
            try:
                tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
                matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name == symbol]
                if len(matches) != 1 or len(matches[0].args.args) != 2:
                    raise ValueError("relocated helper is not a supported two-argument function")
                parameter = matches[0].args.args[1].arg
                recipe = MigrationRecipe(module, symbol, symbol, parameter,
                    f"Require {parameter} by name when calling the relocated {symbol} helper.",
                    ("Preserve header and multipart behavior.",),
                    ("Update all production callers of the helper.",), (), auto_discovered=True)
                config = Config(repo.snapshot, api_migrations=(recipe,),
                    min_caller_modules=1, min_changed_files=1, include_local_calls=True)
                candidates, diagnostics = ApiMigrationDiscovery().discover(repo, config)
                if not candidates:
                    raise ValueError(f"API migration discovery rejected helper: {diagnostics}")
                spec = ApiMigrationSpecBuilder().build(repo, candidates[0], config)
                local_count = spec.metadata["call_counts"].get(path, 0)
                if not local_count:
                    raise ValueError("relocated helper has no local production calls")
                options.append((local_count, symbol, recipe, spec, operation))
            except (OSError, SyntaxError, ValueError, FactoryError) as exc:
                rejected.append({"module": module, "symbol": symbol, "reason": str(exc)})
        if not options:
            raise FactoryError("Historical subtask has no dependent local API migration")
        options.sort(key=lambda item: (-item[0], item[1]))
        chosen = options[0]
        proposals.append((chosen[2], chosen[4]))
        rejected.extend({"module": module_name(item[4]["to_module"], repo.files),
                         "symbol": item[1], "reason": "lower-ranked eligible migration"}
                        for item in options[1:])
    return proposals, rejected


