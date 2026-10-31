"""`/model`: see/select the configured model and edit local IDs.

Selection is by group or by a concrete model ID. Requests use only the selected
ID; a failed request is reported directly with no automatic fallback.
"""

import sys
from collections.abc import Awaitable, Callable
from functools import partial

from cairn.core.model_manager import ModelConfig
from cairn.infra.config import add_model_id, move_model_id, remove_model_id
from cairn.runtime.provider_runtime import UnknownModelError
from cairn.terminal.action import Action, ActionRow, note, refusal
from cairn.terminal.commands.context import CommandContext

MODEL_USAGE = (
    "Usage: /model | /model list | /model use <group|model-id>"
    " | /model add <group> <model-id>"
    " | /model remove <group> <model-id>"
    " | /model move <group> <model-id> <position>"
)
MODEL_SUMMARY = "Show or select a session model, or edit local model IDs."
MODEL_DETAIL = (
    "In a terminal, /model opens the model-group chooser above the composer.\n"
    "Use Up/Down and Enter to select; Esc cancels without changing anything.\n"
    "Without a terminal, /model prints the current group and candidate IDs.\n"
    "/model list shows primary/fallback IDs (* marks the current group).\n"
    "/model use selects a group by name for subsequent requests.\n"
    "Selection preserves conversation history and does not modify configuration.\n"
    "A listed model that failed recently shows its most recent failure.\n"
    "Models and groups belong to the active provider; /provider switches it.\n"
    "/model add appends an ID to an existing group in .cairn/models.toml.\n"
    "/model remove deletes an existing ID, but cannot remove a group's final ID.\n"
    "/model move reorders an ID within its group at a 1-based position.\n"
    "Other IDs keep their relative order; a no-op does not rewrite the file.\n"
    "In a [[providers]] catalog only the active provider's group is edited.\n"
    "Edits preserve comments; unsupported move formatting is rejected.\n"
    "Restart and normal provider approval\n"
    "are required to load changes; the current session is not reloaded.\n"
    "Create TOML first if using legacy .env configuration."
)

#: The keys the chooser answers, shown only while it is open.
CHOOSER_HINT = "Up/Down move   Enter select   Esc cancel"


def _failure_note(context: CommandContext, model_id: str) -> str:
    executor = context.model_executor
    failure = executor.failure_for(model_id) if executor is not None else None
    if failure is None:
        return ""
    detail = f": {failure.detail}" if failure.detail else ""
    return f" — last failure: {failure.category}{detail}"


def _summary(context: CommandContext, model: ModelConfig) -> str:
    """The one line the surface keeps once a group is in use.

    The group, its primary ID and the provider, in that order: those are the three
    facts a reader checks after a switch, and naming them in words keeps the line
    readable without a legend.
    """
    manager = context.model_manager
    current_id = (
        manager.current_model_id() if manager is not None else model.model_ids[0]
    )
    return (
        f"Current model: {current_id} (group: {model.name})"
        f" | provider: {context.active_provider_name or 'unknown'}"
    )


def _details(context: CommandContext, model: ModelConfig) -> list[str]:
    """The selected group's real candidate IDs, primary first."""
    lines = []
    current_id = (
        context.model_manager.current_model_id()
        if context.model_manager is not None
        else model.model_ids[0]
    )
    for position, model_id in enumerate(model.model_ids, start=1):
        marker = "*" if model_id == current_id else " "
        lines.append(
            f" {marker} {position}. {model_id}{_failure_note(context, model_id)}"
        )
    return lines


def _chooser(context: CommandContext, selected: int = 0) -> Action:
    """The chooser over the provider's real groups, with the chosen one expanded."""
    manager = context.model_manager
    assert manager is not None
    current = manager.current_model()
    models = manager.list_models()
    rows = tuple(
        ActionRow(name=model.name, current=model == current) for model in models
    )
    action = Action(
        message="",
        rows=rows,
        selected=selected,
        hint=CHOOSER_HINT,
    ).clamped()
    chosen = action.selected_row()
    model = next((m for m in models if chosen and m.name == chosen.name), current)
    action.message = "\n".join(_details(context, model))
    return action


def _select(context: CommandContext, name: str) -> Action:
    manager = context.model_manager
    assert manager is not None
    current = manager.current_model()
    if name == current.name:
        return note(f"{_summary(context, current)}  (already active)")
    assert context.select_model is not None
    try:
        selected = context.select_model(name)
    except UnknownModelError as exc:
        # The runtime owns the single model lookup; its message is user-facing.
        return refusal(str(exc))
    except Exception:
        # Runtime construction diagnostics can contain provider credentials.
        return refusal("Could not switch model group; the current model is unchanged.")
    return note(_summary(context, selected))


async def _choose(context: CommandContext) -> Action | None:
    """Legacy prompt-based selection, for a caller that supplies a chooser.

    The CLI no longer wires ``choose_model``: the bottom surface is the chooser, so a
    separate prompt would be a second place to make the same decision. This path
    stays for an embedder that has no frame.
    """
    manager = context.model_manager
    assert manager is not None and context.choose_model is not None
    try:
        selected = await context.choose_model(
            context.active_provider_name or "unknown",
            manager.list_models(),
            manager.current_model().name,
        )
    except Exception:
        return refusal(
            "Could not open model selector. Use /model list or /model use <group>."
        )
    if selected is None:
        return None
    return _select(context, selected)


def _has_frame(context: CommandContext) -> bool:
    """Whether this session has a bottom surface to open a chooser in."""
    return context.chooser_available()


def model_chooser_at(context: CommandContext, selected: int) -> Action:
    """The model chooser with ``selected`` chosen and its details expanded."""
    return _chooser(context, selected=selected)


def handle_model(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    """Answer ``/model``: an action for the surface, and any interaction to await.

    A terminal gets the chooser; a script gets the summary, because pressing keys is
    not something a pipeline can do.
    """
    manager = context.model_manager
    if manager is None:
        return refusal("No model configuration is available in this session."), None

    if not args:
        if _has_frame(context):
            return _chooser(context), None
        if sys.stdin.isatty() and context.choose_model and context.select_model:
            return None, partial(_choose, context)
        current = manager.current_model()
        fallbacks = " -> ".join(current.model_ids[1:]) or "none"
        return (
            Action(
                message=(
                    f"{_summary(context, current)}\n"
                    f"Fallbacks: {fallbacks}\n"
                    "Use /model list to see groups, or /model use <group> to switch."
                )
            ),
            None,
        )
    if args == ["list"]:
        print(f"Provider: {context.active_provider_name or 'unknown'}")
        print("Model groups (* current; candidate order shown below):")
        current = manager.current_model()
        for model in manager.list_models():
            marker = "*" if model == current else " "
            print(f"{marker} {model.name}")
            for position, model_id in enumerate(model.model_ids, start=1):
                role = (
                    "current"
                    if model_id == manager.current_model_id()
                    else "configured"
                )
                print(
                    f"    {position}. {model_id} — {role}"
                    f"{_failure_note(context, model_id)}"
                )
        return None, None
    if len(args) == 2 and args[0] == "use" and context.select_model is not None:
        return _select(context, args[1]), None
    if len(args) == 3 and args[0] in {"add", "remove"}:
        try:
            edit = add_model_id if args[0] == "add" else remove_model_id
            edit(args[1], args[2], provider=context.active_provider_name)
        except ValueError as exc:
            return refusal(f"Cannot {args[0]} model: {exc}"), None
        except OSError as exc:
            return (
                refusal(f"Cannot update .cairn/models.toml ({type(exc).__name__})."),
                None,
            )
        return (
            note(
                f"{'Added' if args[0] == 'add' else 'Removed'} {args[2]} "
                f"{'to' if args[0] == 'add' else 'from'} group {args[1]} "
                "in .cairn/models.toml. Restart required; the current session is unchanged."
            ),
            None,
        )
    if len(args) == 4 and args[0] == "move":
        try:
            try:
                position = int(args[3])
            except ValueError:
                raise ValueError("Position must be an integer.") from None
            move_model_id(
                args[1], args[2], position, provider=context.active_provider_name
            )
        except ValueError as exc:
            return refusal(f"Cannot move model: {exc}"), None
        except OSError as exc:
            return (
                refusal(f"Cannot update .cairn/models.toml ({type(exc).__name__})."),
                None,
            )
        return (
            note(
                f"{args[2]} is at position {position} in group {args[1]} "
                "in .cairn/models.toml. Restart required; the current session is unchanged."
            ),
            None,
        )
    return refusal(MODEL_USAGE), None
