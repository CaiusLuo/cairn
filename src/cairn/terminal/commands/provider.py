"""`/provider`: see where the conversation goes, and switch it.

Switching a provider is a trust decision, so the surface states the whole decision
before anything happens: the endpoint the conversation would be sent to, the
credential *variable* it would read, the models it covers, and the fact that the
history moves with it. The credential is named and never resolved until the operator
confirms, and the default is to stay.

The facts live here rather than in a printed paragraph because the reader has to
compare them with what they expected, and a menu that scrolls away cannot be
compared with anything.
"""

from collections.abc import Awaitable, Callable
from functools import partial

from cairn.core.model_manager import ModelConfig
from cairn.core.provider_catalog import NamedProvider
from cairn.terminal.action import Action, ActionRow, note, refusal
from cairn.terminal.commands.context import CommandContext

PROVIDER_USAGE = "Usage: /provider | /provider list | /provider use <name>"
PROVIDER_SUMMARY = "Show or switch the active provider."
PROVIDER_DETAIL = (
    "Show the active provider, its endpoint, credential variable and\n"
    "current model group.\n"
    "/provider list lists configured providers (* marks the active one).\n"
    "/provider use displays the target endpoint, credential variable and\n"
    "models, then requires explicit approval before resolving its credential.\n"
    "Switching transfers the conversation history to the new provider,\n"
    "starts at its first model group and resets session tool-permission\n"
    "grants; history, workspace and traces are preserved.\n"
    "A denied, unavailable or failed switch keeps the current provider."
)

#: The keys the provider chooser answers. Cancel is the default, so it is named.
PROVIDER_HINT = "Up/Down move   Enter switch   Esc cancel"


def _active_provider(context: CommandContext) -> NamedProvider | None:
    catalog = context.provider_catalog
    if catalog is None or context.active_provider_name is None:
        return None
    return catalog.by_name(context.active_provider_name)


def _current_model(context: CommandContext) -> ModelConfig | None:
    manager = context.model_manager
    return manager.current_model() if manager is not None else None


def _decision(provider: NamedProvider) -> str:
    """Every fact the operator needs to judge the switch, and nothing else.

    The endpoint is shown in full because a lookalike host is exactly the thing a
    shortened one would hide; the credential appears as a variable name because
    approving a provider is approving the key it reads.
    """
    group = provider.config.model_config[0]
    return "\n".join(
        [
            f"Send this session's conversation to {provider.name}:",
            f"  {provider.config.base_url}",
            f"Credential variable  {provider.config.api_key_env}",
            f"Models              {', '.join(group.model_ids)}",
            "The full conversation history moves with it, and session tool "
            "permissions are reset.",
        ]
    )


def _chooser(context: CommandContext, selected: int = 0) -> Action:
    """The configured providers, with the target's decision facts spelled out."""
    catalog = context.provider_catalog
    assert catalog is not None
    rows = tuple(
        ActionRow(
            name=provider.name, current=provider.name == context.active_provider_name
        )
        for provider in catalog.providers
    )
    action = Action(message="", rows=rows, selected=selected, hint=PROVIDER_HINT)
    action.clamped()
    chosen = action.selected_row()
    target = next(
        (p for p in catalog.providers if chosen and p.name == chosen.name), None
    )
    action.message = (
        _decision(target)
        if target is not None
        else "No provider is available to switch to."
    )
    return action


def _summary(context: CommandContext) -> Action:
    """The active provider and its routing, as the lines a reader checks.

    Endpoint and credential variable are named because they are what a switch
    changed; the model group follows because a switch also resets it. Printing this
    on the action surface rather than into the transcript keeps a checkable fact off
    the conversation record, where it would be stale by the next turn.
    """
    active = _active_provider(context)
    if active is None:
        return refusal("No provider is active in this session.")
    current = _current_model(context)
    lines = [
        f"Current provider: {active.name}",
        f"Endpoint: {active.config.base_url}",
        f"Credential variable: {active.config.api_key_env}",
    ]
    if current is not None:
        lines.append(
            f"Current model: {current.name} ({' -> '.join(current.model_ids)})"
        )
    return note("\n".join(lines))


def _switch(context: CommandContext, name: str) -> Action:
    if context.select_provider is None:
        return refusal("Provider switching is not available in this session.")
    try:
        context.select_provider(name)
    except ValueError as exc:
        # The runtime refuses with a user-facing reason; the session is unchanged.
        return refusal(str(exc))
    return _summary(context)


async def _approve(context: CommandContext, name: str) -> Action | None:
    """Ask for trust on the surface and report what the switch left in place."""
    assert context.approve_provider is not None
    return await context.approve_provider(name)


def provider_chooser_at(context: CommandContext, selected: int) -> Action:
    """The provider chooser with ``selected`` chosen and its decision shown."""
    return _chooser(context, selected=selected)


def handle_provider(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    """Answer ``/provider`` on the bottom action surface."""
    if len(args) == 2 and args[0] == "use" and context.select_provider is not None:
        if context.approve_provider is not None:
            # Trust is a decision the operator makes on the surface, so the command
            # defers it: the caller awaits the question with the loop still drawing.
            return None, partial(_approve, context, args[1])
        return _switch(context, args[1]), None

    catalog = context.provider_catalog
    if catalog is None:
        return refusal("No provider configuration is available in this session."), None

    if not args:
        if context.chooser_available():
            return _chooser(context), None
        return _summary(context), None

    if args == ["list"]:
        for provider in catalog.providers:
            marker = "*" if provider.name == context.active_provider_name else " "
            print(f"{marker} {provider.name}")
            print(f"    endpoint: {provider.config.base_url}")
            print(f"    credential: {provider.config.api_key_env}")
            print(
                "    groups: "
                + ", ".join(model.name for model in provider.config.model_config)
            )
        return None, None

    return refusal(PROVIDER_USAGE), None
