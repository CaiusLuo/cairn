from cairn.llm.model_manager import ModelConfig
from cairn.llm.provider_catalog import NamedProvider
from cairn.terminal.commands.context import CommandContext

PROVIDER_USAGE = "Usage: /provider | /provider list | /provider use <name>"


def _active_provider(context: CommandContext) -> NamedProvider | None:
    catalog = context.provider_catalog
    if catalog is None or context.active_provider_name is None:
        return None
    return next(
        (
            provider
            for provider in catalog.providers
            if provider.name == context.active_provider_name
        ),
        None,
    )


def _current_model(context: CommandContext) -> ModelConfig | None:
    manager = context.model_manager
    return manager.current_model() if manager is not None else None


def handle_provider(context: CommandContext, args: list[str]) -> None:
    if len(args) == 2 and args[0] == "use" and context.select_provider is not None:
        try:
            context.select_provider(args[1])
        except ValueError as exc:
            print(exc)
            return
        print(f"Current provider: {context.active_provider_name}")
        return

    catalog = context.provider_catalog
    if catalog is None:
        print("No provider configuration is available in this session.")
        return

    if not args:
        active = _active_provider(context)
        if active is None:
            print("No provider is active in this session.")
            return
        current = _current_model(context)
        print(f"Current provider: {active.name}")
        print(f"Endpoint: {active.config.base_url}")
        print(f"Credential variable: {active.config.api_key_env}")
        if current is not None:
            print(f"Current model: {current.name} ({' -> '.join(current.model_ids)})")
        return

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
        return

    print(PROVIDER_USAGE)
