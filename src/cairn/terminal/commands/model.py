import sys
from collections.abc import Awaitable, Callable
from functools import partial

from cairn.config import add_model_id, move_model_id, remove_model_id
from cairn.llm.model_manager import ModelConfig
from cairn.terminal.commands.context import CommandContext

MODEL_USAGE = (
    "Usage: /model | /model list | /model use <name> | /model add <group> <model-id>"
    " | /model remove <group> <model-id>"
    " | /model move <group> <model-id> <position>"
)


def _failure_note(context: CommandContext, model_id: str) -> str:
    executor = context.model_executor
    failure = executor.failure_for(model_id) if executor is not None else None
    if failure is None:
        return ""
    detail = f": {failure.detail}" if failure.detail else ""
    return f" — last failure: {failure.category}{detail}"


def _print_current(context: CommandContext, model: ModelConfig) -> None:
    print(
        f"Current group: {model.name} (primary: {model.model_ids[0]})"
        f" | provider: {context.active_provider_name or 'unknown'}"
    )
    print(f"Fallbacks: {' -> '.join(model.model_ids[1:]) or 'none'}")


def _select(context: CommandContext, name: str) -> None:
    manager = context.model_manager
    assert manager is not None
    if not any(model.name == name for model in manager.list_models()):
        print(f"Model {name!r} not found in the configuration.")
        return
    if name == manager.current_model().name:
        _print_current(context, manager.current_model())
        return
    assert context.select_model is not None
    try:
        selected = context.select_model(name)
    except Exception:
        # Runtime construction diagnostics can contain provider credentials.
        print("Could not switch model group; the current model is unchanged.")
        return
    _print_current(context, selected)


async def _choose(context: CommandContext) -> None:
    manager = context.model_manager
    assert manager is not None and context.choose_model is not None
    try:
        selected = await context.choose_model(
            context.active_provider_name or "unknown",
            manager.list_models(),
            manager.current_model().name,
        )
    except Exception:
        print("Could not open model selector. Use /model list or /model use <group>.")
        return
    if selected is None:
        print("Model selection cancelled.")
        return
    _select(context, selected)


def handle_model(
    context: CommandContext, args: list[str]
) -> Callable[[], Awaitable[None]] | None:
    manager = context.model_manager
    if manager is None:
        print("No model configuration is available in this session.")
        return None

    if not args:
        if sys.stdin.isatty() and context.choose_model and context.select_model:
            return partial(_choose, context)
        _print_current(context, manager.current_model())
        print("Use /model list to see groups, or /model use <group> to switch.")
    elif args == ["list"]:
        print(f"Provider: {context.active_provider_name or 'unknown'}")
        print("Model groups (* current; candidate order shown below):")
        for model in manager.list_models():
            marker = "*" if model == manager.current_model() else " "
            print(f"{marker} {model.name}")
            for position, model_id in enumerate(model.model_ids, start=1):
                role = "primary" if position == 1 else f"fallback {position - 1}"
                print(
                    f"    {position}. {model_id} — {role}{_failure_note(context, model_id)}"
                )
    elif len(args) == 2 and args[0] == "use" and context.select_model is not None:
        _select(context, args[1])
    elif len(args) == 3 and args[0] in {"add", "remove"}:
        try:
            edit = add_model_id if args[0] == "add" else remove_model_id
            edit(args[1], args[2], provider=context.active_provider_name)
        except ValueError as exc:
            print(f"Cannot {args[0]} model: {exc}")
        except OSError as exc:
            print(f"Cannot update .cairn/models.toml ({type(exc).__name__}).")
        else:
            print(
                f"{'Added' if args[0] == 'add' else 'Removed'} {args[2]} "
                f"{'to' if args[0] == 'add' else 'from'} group {args[1]} "
                "in .cairn/models.toml. "
                "Restart required; the current session is unchanged."
            )
    elif len(args) == 4 and args[0] == "move":
        try:
            try:
                position = int(args[3])
            except ValueError:
                raise ValueError("Position must be an integer.") from None
            move_model_id(
                args[1], args[2], position, provider=context.active_provider_name
            )
        except ValueError as exc:
            print(f"Cannot move model: {exc}")
        except OSError as exc:
            print(f"Cannot update .cairn/models.toml ({type(exc).__name__}).")
        else:
            print(
                f"{args[2]} is at position {position} in group {args[1]} "
                "in .cairn/models.toml. "
                "Restart required; the current session is unchanged."
            )
    else:
        print(MODEL_USAGE)
    return None
