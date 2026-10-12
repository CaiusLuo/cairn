"""MemoryStore's host-only mutation API has exactly one caller: MemoryService."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src" / "cairn"
DEFINER = "cairn.infra.memory.store"
OWNER = "cairn.runtime.memory.service"
MUTATORS = frozenset({"insert_entry", "update_entries", "serialize_entry"})


def module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = ("cairn", *relative.parts[:-1])
    if path.name != "__init__.py":
        parts = (*parts, path.stem)
    return ".".join(parts)


def test_only_memory_service_calls_the_store_mutation_api() -> None:
    callers: dict[str, set[str]] = {name: set() for name in MUTATORS}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATORS
            ):
                callers[node.func.attr].add(module_name(path))

    summary = "\n".join(
        f"{name}: {sorted(modules - {DEFINER}) or ['<missing caller>']}"
        for name, modules in sorted(callers.items())
    )
    assert {name: modules - {DEFINER} for name, modules in callers.items()} == {
        name: {OWNER} for name in MUTATORS
    }, (
        f"MemoryStore lifecycle mutations belong to MemoryService ({OWNER}) "
        f"and only MemoryStore itself may reuse its own API:\n{summary}"
    )
