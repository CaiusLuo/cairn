import asyncio
import os
from pathlib import Path

import typer
from dotenv import dotenv_values

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.events import Event
from cairn.core.permissions import SessionPermissionHandler
from cairn.core.provider_catalog import ProviderCatalog
from cairn.infra.config import (
    ConfigLayout,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.storage import TraceStore
from cairn.infra.observability.tracer import Tracer
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from cairn.runtime.provider_runtime import (
    ProviderRuntime,
    ProviderSession,
    build_provider_runtime,
)
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
    StreamingDisplay,
    confirm_provider_access,
    console_event_handler,
    console_permission_prompt,
    print_assistant_response,
    print_banner,
    print_provider_selection,
    print_runtime_error,
)

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
    streaming_display = StreamingDisplay()

    def handle_event(event: Event) -> None:
        nonlocal pending_trace_finish
        if event.type == "trace_finish":
            if event.data.get("persisted", True):
                command_context.last_trace_id = event.data["trace_id"]
            else:
                command_context.last_trace_id = None
            pending_trace_finish = event
            return
        if not streaming_display.observe(event):
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

    def mirror_committed_runtime(runtime: ProviderRuntime) -> None:
        """Keep the command context pointing at the runtime runtime owns."""
        command_context.model_manager = runtime.model_manager
        command_context.model_executor = runtime.model_executor
        command_context.active_provider_name = runtime.provider.name

    # The runtime owns which provider and model are active and every rebinding a
    # switch performs; the CLI keeps prompting, printing and credential lookup.
    session = ProviderSession(
        active_runtime,
        agent=agent,
        on_commit=mirror_committed_runtime,
    )

    def select_provider(name: str) -> None:
        switched = session.select_provider(
            name,
            catalog,
            confirm=lambda provider, group: (
                confirm_provider_access(provider, group, switching=True) is True
            ),
            resolve_credential=lambda config: resolve_provider_api_key(
                config, os.environ, env_file_values
            ),
            context_budget=context_budget,
        )
        if switched:
            permission_handler.reset_grants()

    command_context.select_model = session.select_model
    command_context.select_provider = select_provider
    command_context.provider_catalog = catalog
    # Commands run between turns only, so a switch can never race a turn.

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

        streaming_display.reset()
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
            if not streaming_display.final_text_visible:
                print_assistant_response(response)
        finally:
            streaming_display.close_incomplete()
            if pending_trace_finish is not None:
                console_event_handler(pending_trace_finish)
                pending_trace_finish = None

    print("Goodbye! see you next time.")


if __name__ == "__main__":
    app()
