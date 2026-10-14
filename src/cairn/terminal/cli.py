import asyncio
import os
from pathlib import Path
from typing import Annotated

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
from cairn.runtime.agent import Agent
from cairn.runtime.assembly import build_agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.loop import run_turn
from cairn.runtime.provider_runtime import (
    ProviderRuntime,
    ProviderSession,
    build_provider_runtime,
)
from cairn.runtime.session import SessionState
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.durable import (
    DurableOptions,
    attach_session,
    inspect_session,
    reconcile_session,
    show_report,
)
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

session_app = typer.Typer(
    help="Create, inspect, reopen or reconcile local conversations."
)
app.add_typer(session_app, name="session")
StoreOption = Annotated[
    Path | None, typer.Option("--store", help="Session storage directory.")
]
RootsOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--forbid-root", help="Additional host sandbox-writable root (repeatable)."
    ),
]


def _durable_command(options: DurableOptions) -> None:
    try:
        asyncio.run(main(durable=options))
    except Exception:
        typer.echo(
            "Durable session unavailable or binding rejected. Inspect before recovery.",
            err=True,
        )
        raise typer.Exit(1) from None


@session_app.command("new")
def session_new(store: StoreOption = None, forbid_root: RootsOption = None) -> None:
    """Create a durable conversation and enter the CLI."""
    _durable_command(
        DurableOptions(
            "new", directory=store, additional_roots=tuple(forbid_root or ())
        )
    )


@session_app.command("inspect")
def session_inspect(
    session_id: str, store: StoreOption = None, forbid_root: RootsOption = None
) -> None:
    """Read bounded recovery metadata without mutation or provider access."""
    try:
        inspect_session(session_id, store, tuple(forbid_root or ()))
    except Exception:
        typer.echo(
            "Session inspection unavailable; verify ID, store and writable roots.",
            err=True,
        )
        raise typer.Exit(1) from None


@session_app.command("reopen")
def session_reopen(
    session_id: str, store: StoreOption = None, forbid_root: RootsOption = None
) -> None:
    """Reopen with fresh provider approval and explicit binding acknowledgement."""
    _durable_command(
        DurableOptions("reopen", session_id, store, tuple(forbid_root or ()))
    )


@session_app.command("reconcile")
def session_reconcile(
    session_id: str, store: StoreOption = None, forbid_root: RootsOption = None
) -> None:
    """Explicitly abandon an unfinished turn without replay, then exit."""
    _durable_command(
        DurableOptions("reconcile", session_id, store, tuple(forbid_root or ()))
    )


async def _durable_turn(agent: Agent, user_input: str, session: DurableSession) -> str:
    task = asyncio.create_task(
        run_turn(
            agent, user_input=user_input, budget=DEFAULT_CLI_RUN_BUDGET, session=session
        )
    )
    try:
        return await asyncio.shield(task)
    except BaseException:
        # The CLI must join execution/cleanup before closing the writer, even
        # through repeated caller cancellation. EOF is read only between turns.
        await settle_owned(task)
        raise


@app.command("tui")
def tui(
    ui_dir: Annotated[Path | None, typer.Option(help="Built ui/pi directory.")] = None,
) -> None:
    """Launch the experimental Pi terminal UI."""
    from cairn.terminal.tui import launch

    launch(ui_dir)


@app.callback()
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(main())


async def main(
    cli_input: CliInput | None = None, *, durable: DurableOptions | None = None
) -> None:
    env_file_values = dotenv_values()
    model_path = Path(".cairn/models.toml")
    project = load_project_providers(model_path)

    provider = default_provider(project, os.environ, env_file_values)
    selected_group = provider.config.model_config[0]
    if durable is not None or project.layout is not ConfigLayout.ENV:
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
        if durable is not None:
            notices = {
                "tool_call": "Provisional tool execution; checkpoint not committed.",
                "tool_result": "Provisional tool observation; checkpoint not committed.",
                "tool_error": "Tool failed; checkpoint not committed.",
                "tool_denied": "Tool permission denied.",
                "agent_finish": "Turn output finished; awaiting durable checkpoint.",
            }
            if event.type in notices:
                if event.type == "agent_finish":
                    streaming_display.observe(event)
                print(notices[event.type])
                return
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

    durable_session: DurableSession | None = None
    if durable is not None:
        durable_session, observe_binding = attach_session(
            durable,
            agent,
            workspace,
            active_runtime,
            max_steps=DEFAULT_CLI_RUN_BUDGET.max_steps,
            secret_env_keys=secret_env_keys,
            sensitive_values=tuple(
                {
                    api_key,
                    *(
                        os.environ.get(k) or env_file_values.get(k) or ""
                        for k in secret_env_keys
                    ),
                }
                - {""}
            ),
        )
    try:
        if durable_session is not None:
            assert durable is not None
            if durable.operation == "new":
                show_report(durable_session.inspect())
            elif not reconcile_session(
                durable_session,
                observe_binding,
                allow_unfinished=durable.operation == "reconcile",
            ):
                return
            if durable.operation == "reconcile":
                return
            if durable_session.recovery_notice:
                print(durable_session.recovery_notice)
            print(
                "Durable mode: output is provisional until a committed checkpoint status."
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
            if durable_session is not None and user_input.split()[0].lower() in {
                "/model",
                "/provider",
            }:
                print(
                    "Model/provider switching is disabled in durable mode. Exit and reopen with fresh approval to acknowledge current configuration."
                )
                continue
            command_result = router.handle(user_input, command_context)
            if command_result.handled:
                if command_result.interaction is not None:
                    await command_result.interaction()
                if command_result.should_exit:
                    break
                continue
            streaming_display.reset()
            previous_seq = (
                durable_session.receipt.revision.head_seq if durable_session else None
            )
            try:
                if durable_session is None:
                    response = await run_turn(
                        agent, user_input=user_input, budget=DEFAULT_CLI_RUN_BUDGET
                    )
                else:
                    response = await _durable_turn(agent, user_input, durable_session)
            except RunBudgetExceeded:
                pass
            except Exception as exc:
                if durable_session is None:
                    print_runtime_error(exc)
                else:
                    print(
                        "Durable turn failed. Check committed status; inspect before recovery."
                    )
            else:
                if not streaming_display.final_text_visible:
                    print_assistant_response(response)
            finally:
                streaming_display.close_incomplete()
                if pending_trace_finish is not None:
                    if (
                        durable_session is not None
                        and not pending_trace_finish.data.get("persisted", True)
                    ):
                        print("Trace unavailable: persistence failed.")
                    else:
                        console_event_handler(pending_trace_finish)
                    pending_trace_finish = None
            if durable_session is not None:
                if durable_session.state is not SessionState.READY:
                    print(
                        "Session stopped: recovery required. Use cairn session inspect/reconcile before new turns."
                    )
                    break
                receipt = durable_session.receipt
                if receipt.revision.head_seq != previous_seq:
                    print(
                        f"Committed checkpoint {receipt.revision.head_seq} ({receipt.outcome})."
                    )

        print("Goodbye! see you next time.")
    finally:
        if durable_session is not None:
            durable_session.close()


if __name__ == "__main__":
    app()
