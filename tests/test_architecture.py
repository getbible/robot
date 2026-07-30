import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Dependencies may point to the same or a lower layer. Same-layer dependencies
# are allowed for cohesive adapters, but the separate cycle test prevents them
# from becoming mutually recursive.
LAYERS = {
    "config": 0,
    "container.__init__": 0,
    "container.runtime": 0,
    "modules.__init__": 0,
    "modules.audit": 0,
    "modules.cache_maintenance": 0,
    "modules.catalog": 0,
    "modules.ephemeral": 0,
    "modules.errors": 0,
    "modules.miniapp_auth": 0,
    "modules.preferences": 0,
    "modules.runtime_notify": 0,
    "modules.utils": 0,
    "modules.interactions": 1,
    "modules.rate_limit": 1,
    "modules.renderer": 1,
    "modules.miniapp_sessions": 2,
    "modules.service": 2,
    "modules.health": 3,
    "modules.miniapp_cleanup": 3,
    "modules.posting": 3,
    "modules.miniapp_api": 4,
    "modules.commands": 5,
    "modules.dependencies": 5,
    "modules.miniapp_tornado": 5,
    "bot": 6,
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _runtime_modules() -> dict[str, Path]:
    paths = [
        ROOT / "bot.py",
        ROOT / "config.py",
        *(ROOT / "modules").glob("*.py"),
        *(ROOT / "container").glob("*.py"),
    ]
    return {_module_name(path): path for path in paths}


def _local_dependencies(module: str, path: Path) -> set[str]:
    dependencies: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                candidates = (node.module or "",)
            else:
                parent = package.split(".") if package else []
                retained = parent[: max(0, len(parent) - node.level + 1)]
                relative = (node.module or "").split(".") if node.module else []
                candidates = (".".join((*retained, *relative)),)
        else:
            continue
        for candidate in candidates:
            if candidate in LAYERS:
                dependencies.add(candidate)
    return dependencies


class ArchitectureBoundaryTestCase(unittest.TestCase):
    def test_every_runtime_module_has_an_explicit_owner_layer(self) -> None:
        modules = _runtime_modules()
        self.assertEqual(
            set(LAYERS),
            set(modules),
            (
                "Add every new runtime module to the documented architecture "
                "layer before merging it."
            ),
        )

    def test_dependencies_never_point_toward_an_outer_layer(self) -> None:
        violations: list[str] = []
        for module, path in sorted(_runtime_modules().items()):
            for dependency in sorted(_local_dependencies(module, path)):
                if LAYERS[dependency] > LAYERS[module]:
                    violations.append(
                        f"{module} (layer {LAYERS[module]}) imports "
                        f"{dependency} (layer {LAYERS[dependency]})"
                    )
        self.assertEqual(violations, [])

    def test_runtime_module_graph_is_acyclic(self) -> None:
        modules = _runtime_modules()
        graph = {
            module: _local_dependencies(module, path)
            for module, path in modules.items()
        }
        visited: set[str] = set()
        active: list[str] = []
        cycles: list[str] = []

        def visit(module: str) -> None:
            if module in active:
                start = active.index(module)
                cycles.append(" -> ".join((*active[start:], module)))
                return
            if module in visited:
                return
            active.append(module)
            for dependency in sorted(graph[module]):
                visit(dependency)
            active.pop()
            visited.add(module)

        for module in sorted(graph):
            visit(module)
        self.assertEqual(cycles, [])


if __name__ == "__main__":
    unittest.main()
