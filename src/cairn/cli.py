import asyncio
import os
from dataclasses import replace
from pathlib import Path

import typer
from dotenv import dotenv_values

from cairn.assembly import build_agent
from cairn.config import CAIRN_CONFIG_ENV_NAMES as CAIRN_CONFIG_ENV_NAMES
from cairn.config import (
    ConfigLayout,
    default_provider,
    load_project_providers,
    resolve_provider_api_key,
)
from cairn.config import resolve_cairn_config as resolve_cairn_config
from cairn.config import resolve_context_budget as resolve_context_budget
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.permissions import SessionPermissionHandler
from cairn.llm.model_manager import ModelConfig
from cairn.llm.provider_catalog import ProviderCatalog
from cairn.llm.provider_runtime import ProviderRuntime, build_provider_runtime
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.storage import TraceStore
from cairn.observability.tracer import Tracer
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
    confirm_provider_access,
    console_event_handler,
    console_permission_prompt,
    print_assistant_response,
    print_banner,
    print_provider_selection,
    print_runtime_error,
)
from cairn.workspace.workspace import Workspace

app = typer.Typer(
    invoke_without_command=True,
    pretty_exceptions_show_locals=False,
)

DEFAULT_CLI_RUN_BUDGET = RunBudget(max_steps=50)


@app.callback()
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(main())


async def main(cli_input: CliInput | None = None) -> None:
    env_file_values = dotenv_values()
    model_path = Path(".cairn/models.toml")
    project = load_project_providers(model_path)

    provider = default_provider(project, os.environ, env_file_values)
    selected_group = provider.config.model_config[0]
    if project.layout is not ConfigLayout.ENV:
        if (
            confirm_provider_access(provider, selected_group, switching=False)
            is not True
        ):
            raise ValueError(
                "Project model provider was not approved for this session."
            )
        print_provider_selection(provider, selected_group)

    # Only the selected provider's credential is resolved, and only after its
    # configuration was validated and approved.
    api_key = resolve_provider_api_key(provider.config, os.environ, env_file_values)
    context_budget = resolve_context_budget(os.environ, env_file_values)
    active_runtime = build_provider_runtime(
        provider, api_key, context_budget, group=selected_group.name
    )

    print_banner()

    workspace = Workspace(Path.cwd())
    trace_root = Path(".cairn/traces")
    tracer = Tracer(JsonlTraceSink(trace_root))
    command_context = CommandContext(trace_store=TraceStore(trace_root))
    permission_handler = SessionPermissionHandler(prompt=console_permission_prompt)
    # The .env layout declares no catalog; its implicit provider is the only one.
    catalog = ProviderCatalog(providers=project.providers or (provider,))
    # Every configured credential variable is withheld from Bash children,
    # including providers this session has not selected.
    secret_env_keys = frozenset(named.config.api_key_env for named in catalog.providers)

    pending_trace_finish: Event | None = None

    def handle_event(event: Event) -> None:
        nonlocal pending_trace_finish
        if event.type == "trace_finish":
            if event.data.get("persisted", True):
                command_context.last_trace_id = event.data["trace_id"]
            else:
                command_context.last_trace_id = None
            pending_trace_finish = event
            return
        console_event_handler(event)

    agent = build_agent(
        workspace=workspace,
        llm=active_runtime.llm,
        event_handler=handle_event,
        permission_handler=permission_handler,
        tracer=tracer,
        context_builder=active_runtime.context_builder,
        secret_env_keys=secret_env_keys,
        model_executor=active_runtime.model_executor,
    )

    def activate(runtime: ProviderRuntime) -> None:
        """Commit one fully built provider runtime to every session holder."""
        nonlocal active_runtime
        active_runtime = runtime
        agent.llm = runtime.llm
        agent.context_builder = runtime.context_builder
        agent.model_executor = runtime.model_executor
        agent.provider_name = runtime.provider.name
        command_context.model_manager = runtime.model_manager
        command_context.model_executor = runtime.model_executor
        command_context.active_provider_name = runtime.provider.name

    def select_model(name: str) -> ModelConfig:
        runtime = active_runtime
        manager = runtime.model_manager
        previous = manager.current_model()
        if name == previous.name:
            return previous
        selected = next(
            (model for model in manager.list_models() if model.name == name), None
        )
        if selected is None:
            raise ValueError(f"Model {name!r} not found in the configuration.")
        next_llm, next_context = runtime.runtime_factory(selected.model_ids[0])
        manager.select_model(name)
        # The CLI handles commands between turns. No await separates the pair,
        # and the existing Agent (state, tools, permissions and tracer) is kept.
        activate(replace(runtime, llm=next_llm, context_builder=next_context))
        return selected

    def select_provider(name: str) -> None:
        runtime = active_runtime
        target = next(
            (named for named in catalog.providers if named.name == name), None
        )
        if target is None:
            raise ValueError(f"Provider {name!r} is not configured in this session.")
        if target.name == runtime.provider.name:
            # Already active: keep the runtime, selection and grants untouched.
            return

        group = target.config.model_config[0]
        if confirm_provider_access(target, group, switching=True) is not True:
            raise ValueError(f"Provider {name!r} was not approved for this session.")
        credential = resolve_provider_api_key(
            target.config, os.environ, env_file_values
        )
        # Build the target completely before committing anything: a construction
        # failure leaves the active provider, its selection and its grants usable.
        try:
            candidate = build_provider_runtime(
                target, credential, context_budget, group=group.name
            )
        except Exception as exc:
            # Never echo a construction diagnostic that may embed a credential.
            raise ValueError(
                f"Provider {name!r} could not be activated "
                f"({type(exc).__name__}); the current provider is unchanged."
            ) from None
        activate(candidate)
        permission_handler.reset_grants()

    command_context.select_model = select_model
    command_context.select_provider = select_provider
    command_context.provider_catalog = catalog
    # Commands run between turns only, so a switch can never race a turn.
    activate(active_runtime)

    router = CommandRouter()
    input_reader = cli_input or CliInput()
    command_context.choose_model = lambda provider, models, current: (
        input_reader.choose_model(provider, models, current)
    )

    while True:
        try:
            user_input = await input_reader.read()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break

        if not user_input or user_input.isspace():
            continue

        command_result = router.handle(
            user_input,
            command_context,
        )

        if command_result.handled:
            if command_result.interaction is not None:
                await command_result.interaction()
            if command_result.should_exit:
                break
            continue

        try:
            response = await run_turn(
                agent,
                user_input=user_input,
                budget=DEFAULT_CLI_RUN_BUDGET,
            )
        except RunBudgetExceeded:
            pass
        except Exception as exc:
            print_runtime_error(exc)
        else:
            print_assistant_response(response)
        finally:
            if pending_trace_finish is not None:
                console_event_handler(pending_trace_finish)
                pending_trace_finish = None

    print("Goodbye! see you next time.")


if __name__ == "__main__":
    app()
