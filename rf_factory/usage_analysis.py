"""Small, conservative Python import graph for API migration candidates.

Recognizes absolute imports, re-exports, and module-qualified calls. Dynamic
imports and rebinding are outside this strategy and must not be silently used.
This module is copied beside the portable verifier.
"""
import ast
from collections import deque
from pathlib import Path


def module_name(path):
    name = path.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


class UsageIndex:
    def __init__(self, root, files, api_module, api_symbol):
        self.root = Path(root)
        self.trees = {}
        self.paths = {}
        for path in sorted(files):
            if path.endswith(".py") and not any(part in {"tests", "test"} for part in Path(path).parts):
                module = module_name(path)
                self.trees[module] = ast.parse((self.root / path).read_text(encoding="utf-8"))
                self.paths[module] = path
        self.api_module = api_module
        self.api_symbol = api_symbol
        self._export_cache = {}

    def exports(self, module, symbol, seen=frozenset()):
        if (module, symbol) == (self.api_module, self.api_symbol):
            return True
        if module not in self.trees or (module, symbol) in seen:
            return False
        for node in self.trees[module].body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if (alias.asname or alias.name) == symbol and self.exports(
                            node.module, alias.name, seen | {(module, symbol)}):
                        return True
        return False

    def bindings(self, module):
        result = {}
        for node in self.trees[module].body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result[alias.asname or alias.name.split(".")[0]] = ("module", alias.name if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if self.exports(node.module, alias.name):
                        result[local] = ("function", self.api_module)
                    elif node.module + "." + alias.name in self.trees:
                        result[local] = ("module", node.module + "." + alias.name)
        return result

    def resolve(self, node, bindings):
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.resolve(node.value, bindings)
            if parent and parent[0] == "module":
                module = parent[1]
                if self.exports(module, node.attr):
                    return ("function", self.api_module)
                if module + "." + node.attr in self.trees:
                    return ("module", module + "." + node.attr)
        return None

    def calls(self, module):
        bindings = self.bindings(module)
        return [node for node in ast.walk(self.trees[module]) if isinstance(node, ast.Call)
                and self.resolve(node.func, bindings) == ("function", self.api_module)]

    def call_sites(self):
        return {self.paths[module]: self.calls(module) for module in self.trees
                if module != self.api_module and self.calls(module)}

    def dependencies(self, module):
        result = set()
        for node in self.trees[module].body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.trees:
                        result.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in self.trees:
                    result.add(node.module)
                for alias in node.names:
                    child = node.module + "." + alias.name
                    if child in self.trees:
                        result.add(child)
        return result

    def dependency_depth(self):
        """Longest shortest import path from a production module to the API."""
        depths = []
        for start in self.trees:
            queue = deque([(start, 0)])
            seen = set()
            while queue:
                module, depth = queue.popleft()
                if module in seen:
                    continue
                seen.add(module)
                if module == self.api_module:
                    depths.append(depth)
                    break
                queue.extend((dep, depth + 1) for dep in self.dependencies(module) if dep not in seen)
        return max(depths, default=0)
