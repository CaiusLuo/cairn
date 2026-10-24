import asyncio
import os
import sys
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget
from cairn.core.events import Event, EventHandler
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.core.provider_catalog import ProviderCatalog
from cairn.infra.config import (
    CAIRN_MAX_STEPS_ENV_NAME,
    ConfigLayout,
    ProjectProviders,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.storage import TraceStore
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.skills import builtin_skills
from cairn.infra.tools.filesystem_access import normalize_access_root
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
from cairn.runtime.turn.compaction import CompactionOutcome
from cairn.runtime.turn.hook import run_compaction
from cairn.runtime.turn.steering import PendingMessage, SteerQueue
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.durable import (
    DurableOptions,
    attach_session,
    inspect_session,
    list_sessions,
    reconcile_session,
    show_report,
)
from cairn.terminal.frame import TranscriptView, frame_live_region
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
    StreamingDisplay,
    confirm_provider_access,
    confirm_workspace_trust,
    console_event_handler,
    console_permission_prompt,
    print_assistant_response,
    print_banner,
    print_provider_selection,
    print_runtime_error,
    print_user_message,
)
from cairn.terminal.submission import PendingLine

app = typer.Typer(
    invoke_without_command=True,
    pretty_exceptions_show_locals=False,
)

DEFAULT_CLI_MAX_STEPS = 150


#: One owner for the per-turn step ceiling. ``CAIRN_MAX_STEPS`` overrides it so a
#: long task can be admitted without editing source; invalid values fail loudly
#: instead of silently falling back to a different budget.
def resolve_cli_run_budget(environ: Mapping[str, str] | None = None) -> RunBudget:
    source = os.environ if environ is None else environ
    raw = source.get(CAIRN_MAX_STEPS_ENV_NAME)
    if raw is None or not raw.strip():
        return RunBudget(max_steps=DEFAULT_CLI_MAX_STEPS)
    try:
        max_steps = int(raw.strip())
    except ValueError:
        raise typer.BadParameter(
            f"{CAIRN_MAX_STEPS_ENV_NAME} must be a positive integer"
        ) from None
    if max_steps < 1:
        raise typer.BadParameter(
            f"{CAIRN_MAX_STEPS_ENV_NAME} must be a positive integer"
        )
    return RunBudget(max_steps=max_steps)


#: Resolved once at import so callers and tests observe the same budget object.
DEFAULT_CLI_RUN_BUDGET = resolve_cli_run_budget()

session_app = typer.Typer(
    help="Create, inspect, reopen or reconcile local conversations."
)
app.add_typer(session_app, name="session")
StoreOption = Annotated[
    Path | None, typer.Option("--store", help="Session storage directory.")
]
RootsOption = Annotated[
    list[Path] | None,
    typer.Option("--forbid-root", help="Additional host-writable root (repeatable)."),
]


def _durable_command(options: DurableOptions) -> None:
    try:
        asyncio.run(main(durable=options))
    except SessionStoreError as refused:
        # A runtime the store refused is actionable: report why, and never
        # describe the durable state as rejected.
        typer.echo(
            f"Durable execution unavailable: {refused}",
            err=True,
        )
        raise typer.Exit(1) from None
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


@session_app.command("list")
def session_list(store: StoreOption = None, forbid_root: RootsOption = None) -> None:
    """List the stored conversations that can be reopened, newest first."""
    try:
        list_sessions(store, tuple(forbid_root or ()))
    except SessionStoreError as refused:
        typer.echo(f"Session listing unavailable: {refused}", err=True)
        raise typer.Exit(1) from None
    except Exception:
        typer.echo(
            "Session listing unavailable; verify the store and writable roots.",
            err=True,
        )
        raise typer.Exit(1) from None


@session_app.command("inspect")
def session_inspect(
    session_id: str, store: StoreOption = None, forbid_root: RootsOption = None
) -> None:
    """Read bounded recovery metadata without mutation or provider access."""
    try:
        inspect_session(session_id, store, tuple(forbid_root or ()))
    except SessionStoreError as refused:
        typer.echo(f"Session inspection unavailable: {refused}", err=True)
        raise typer.Exit(1) from None
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


@app.callback()
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(main())


def _durable_notices(durable: DurableOptions | None) -> dict[str, str]:
    return {
        "tool_call": "Tool execution in progress; checkpoint not committed yet.",
        "tool_result": "Tool finished; checkpoint not committed yet.",
        "tool_error": "Tool failed; checkpoint not committed.",
        "tool_denied": "Tool permission denied.",
        "agent_finish": "Turn output finished; awaiting durable checkpoint.",
    }


def _make_event_handler(
    durable: DurableOptions | None,
    streaming_display: StreamingDisplay,
    command_context: CommandContext,
) -> tuple[EventHandler, list[Event | None]]:
    """Build the CLI event handler and the shared pending-trace slot.

    The slot is a one-element list so the turn loop can read and clear the
    pending `trace_finish` without a second closure.
    """
    pending_trace_finish: list[Event | None] = [None]
    notices = _durable_notices(durable) if durable is not None else {}

    def handle_event(event: Event) -> None:
        if durable is not None and event.type in notices:
            if event.type == "agent_finish":
                streaming_display.observe(event)
            print(notices[event.type])
            return
        if event.type == "trace_finish":
            if event.data.get("persisted", True):
                command_context.last_trace_id = event.data["trace_id"]
            else:
                command_context.last_trace_id = None
            # The display reads the turn's totals from this event, so it is
            # offered before the turn loop files it away for the closed turn.
            # A durable turn reports an unstored trace in its own words, so the
            # console handler must not say it again.
            streaming_display.observe(event)
            console_event_handler(event, report_trace_failure=durable is None)
            pending_trace_finish[0] = event
            return
        # The context budget arrives with the step and each step's measurement with
        # the stream end, both on existing event types: the status line needs
        # data, not a new kind of event for other consumers to learn.
        if event.type == "agent_step":
            limit = _optional_count(event.data.get("context_limit"))
            if limit is not None:
                streaming_display.observe_context(used=None, limit=limit)
        if event.type == "model_stream_end" and event.data.get("status") == "completed":
            streaming_display.observe_context(
                used=_optional_count(event.data.get("context_used")), limit=None
            )
            streaming_display.observe_step_usage(
                input_tokens=_optional_count(event.data.get("input_tokens")),
                output_tokens=_optional_count(event.data.get("output_tokens")),
            )
        if not streaming_display.observe(event):
            console_event_handler(event)

    return handle_event, pending_trace_finish


def _optional_count(value: object) -> int | None:
    """A token count when the provider reported one, otherwise nothing."""
    return value if type(value) is int and value >= 0 else None


def _resolve_active_runtime(
    durable: DurableOptions | None,
    project: ProjectProviders,
    env_file_values: dict[str, str | None],
) -> tuple[ProviderRuntime, str, ContextBudget]:
    """Approve the selected provider, then resolve only its credential.

    Returns the runtime plus the resolved credential and context budget, which
    the caller still needs for durable attachment and later provider switches.
    """
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
    runtime = build_provider_runtime(
        provider, api_key, context_budget, group=selected_group.name
    )
    return runtime, api_key, context_budget


async def _run_supervised_turn(
    *,
    agent: Agent,
    user_input: str,
    frame: TranscriptView | None,
    input_reader: CliInput,
    durable_session: DurableSession | None,
) -> str:
    """Run one turn, watching for a message typed while it runs.

    Without a frame there is no input line to read from during a turn, so the turn
    runs alone. With one, the input keeps running and a typed line either waits for
    the turn or interrupts it: an interruption cancels the turn and becomes the
    guidance of the next one, which keeps ``run_turn`` the only Agent turn.
    """

    if frame is None:
        if durable_session is None:
            return await run_turn(
                agent, user_input=user_input, budget=DEFAULT_CLI_RUN_BUDGET
            )
        return await _durable_turn(agent, user_input, durable_session)

    # The input line keeps running while the turn does. A message typed now either
    # joins this turn at its next step boundary (steer) or waits for it to end
    # (queue); neither cancels the turn, because a redirect is meant to keep the
    # work already done.
    steering = SteerQueue()
    return await _drive_turn(
        agent=agent,
        user_input=user_input,
        steering=steering,
        frame=frame,
        durable_session=durable_session,
    )


async def _drive_turn(
    *,
    agent: Agent,
    user_input: str,
    steering: SteerQueue,
    frame: TranscriptView,
    durable_session: DurableSession | None,
) -> str:
    """Run one turn while reading further instructions from the input line."""

    async def run() -> str:
        if durable_session is None:
            return await run_turn(
                agent,
                user_input=user_input,
                budget=DEFAULT_CLI_RUN_BUDGET,
                steering=steering,
            )
        return await _durable_turn(agent, user_input, durable_session)

    turn = asyncio.create_task(run())
    reader = asyncio.create_task(frame.read())
    try:
        while True:
            done, _ = await asyncio.wait(
                {turn, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if turn in done:
                return await turn
            line = await reader
            if line.text.strip():
                # ``now`` only applies when nothing is running; during a turn the
                # two meaningful choices are joining it or waiting for it.
                steering.push(
                    PendingMessage(
                        text=line.text,
                        delivery="queue" if line.delivery == "queue" else "steer",
                    )
                )
            reader = asyncio.create_task(frame.read())
    finally:
        for task in (turn, reader):
            if not task.done():
                task.cancel()
        await asyncio.gather(turn, reader, return_exceptions=True)


def _interactive_terminal() -> bool:
    """Whether this process has a terminal the frame may own."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _print_durable_intro(durable_session: DurableSession) -> None:
    if durable_session.recovery_notice:
        print(durable_session.recovery_notice)
    print("Durable mode: output is provisional until a committed checkpoint status.")


async def _turn_loop(
    *,
    agent: Agent,
    input_reader: CliInput,
    frame: TranscriptView | None,
    router: CommandRouter,
    command_context: CommandContext,
    streaming_display: StreamingDisplay,
    pending_trace_finish: list[Event | None],
    durable_session: DurableSession | None,
) -> None:
    """Read input, dispatch commands, and run turns until exit."""
    while True:
        try:
            # The frame owns the input line when it exists, so typed text and the
            # live region share one viewport instead of fighting for the cursor.
            submitted = (
                await frame.read() if frame is not None else await input_reader.read()
            )
            # This loop only reads while the session is idle, so the delivery a line
            # asked for does not apply here: work starts now either way.
            user_input = (
                submitted.text if isinstance(submitted, PendingLine) else submitted
            )
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
        if frame is not None:
            frame.set_running(True)
        # Echo the prompt before the answer so the transcript keeps both sides of
        # the conversation; the reply alone leaves the reader reconstructing what
        # was asked.
        print_user_message(user_input)
        previous_seq = (
            durable_session.receipt.revision.head_seq if durable_session else None
        )
        try:
            response = await _run_supervised_turn(
                agent=agent,
                user_input=user_input,
                frame=frame,
                input_reader=input_reader,
                durable_session=durable_session,
            )
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
            if frame is not None:
                frame.set_running(False)
            streaming_display.close_incomplete()
            pending = pending_trace_finish[0]
            if pending is not None:
                if durable_session is not None and not pending.data.get(
                    "persisted", True
                ):
                    print("Trace unavailable: persistence failed.")
                else:
                    console_event_handler(pending)
                pending_trace_finish[0] = None
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


async def main(
    cli_input: CliInput | None = None, *, durable: DurableOptions | None = None
) -> None:
    env_file_values = dotenv_values()
    model_path = Path(".cairn/models.toml")
    project = load_project_providers(model_path)
    active_runtime, api_key, context_budget = _resolve_active_runtime(
        durable, project, env_file_values
    )

    print_banner()

    workspace = Workspace(Path.cwd())
    trace_root = Path(".cairn/traces")
    tracer = Tracer(JsonlTraceSink(trace_root))
    command_context = CommandContext(trace_store=TraceStore(trace_root))
    permission_handler = SessionPermissionHandler(
        prompt=console_permission_prompt,
        workspace_root=workspace.root,
        home=Path.home(),
        root_normalizer=normalize_access_root,
    )
    # A local Cairn edits the directory it was started in, so that directory is
    # trusted explicitly before the first turn. Denial — or no interactive
    # terminal — stops here rather than starting a session it cannot use.
    if confirm_workspace_trust(workspace.root) is not True:
        raise ValueError("Workspace access was not approved for this session.")
    permission_handler.grants.add(PermissionCapability.WORKSPACE_TRUST)
    # The .env layout declares no catalog; its implicit provider is the only one.
    catalog = ProviderCatalog(providers=project.providers or (active_runtime.provider,))
    # Every configured credential variable is withheld from Bash children,
    # including providers this session has not selected.
    secret_env_keys = frozenset(named.config.api_key_env for named in catalog.providers)

    # The live region is drawn inside the frame when there is an interactive
    # terminal to draw it in, and by rich otherwise. The two cannot share a
    # terminal: a rich region moves the cursor itself, which the frame's
    # application repaints over. Off terminal there is nothing to repaint, and the
    # display already falls back to printing each block once.
    frame = TranscriptView() if _interactive_terminal() else None
    streaming_display = StreamingDisplay(
        live_factory=frame_live_region(frame) if frame is not None else None,
        # Session facts belong under the input when there is one: they are read
        # while waiting, and a line per measurement would crowd the transcript.
        on_status=frame.observe_status if frame is not None else None,
    )
    # Which model this session talks to is a fact about the session, not about the
    # turn, so it is set once here rather than derived from each event.
    streaming_display.observe_model(
        active_runtime.model_manager.current_model().model_ids[0],
        provider=active_runtime.provider.name,
    )
    if frame is not None:
        # Ctrl+O reveals more reasoning. A keystroke rather than a click: capturing
        # the mouse would cost the native drag-select and copy this layout exists to
        # keep.
        frame.on_reveal_reasoning = streaming_display.reveal_reasoning
    handle_event, pending_trace_finish = _make_event_handler(
        durable, streaming_display, command_context
    )

    agent = build_agent(
        workspace=workspace,
        llm=active_runtime.llm,
        event_handler=handle_event,
        permission_handler=permission_handler,
        tracer=tracer,
        context_builder=active_runtime.context_builder,
        secret_env_keys=secret_env_keys,
        model_executor=active_runtime.model_executor,
        skills=builtin_skills(),
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

    def compact_now() -> Awaitable[CompactionOutcome]:
        """Compact the live Agent on the user's explicit request."""
        # The command runs between turns, so the durable binding already exists
        # and this measurement must carry the same system context the turn does.
        return run_compaction(agent, session=durable_session)

    # Only a session that actually summarizes can compact; an Agent built without
    # a summarizer leaves the command unavailable rather than silently doing
    # nothing.
    command_context.compact_now = compact_now if agent.summarizer is not None else None
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
        durable_session, observe_binding = await attach_session(
            durable,
            agent,
            workspace,
            active_runtime,
            max_steps=DEFAULT_CLI_RUN_BUDGET.max_steps,
            choose_session=input_reader.choose_session,
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
            _print_durable_intro(durable_session)
        await _turn_loop(
            agent=agent,
            input_reader=input_reader,
            frame=frame,
            router=router,
            command_context=command_context,
            streaming_display=streaming_display,
            pending_trace_finish=pending_trace_finish,
            durable_session=durable_session,
        )
        print("Goodbye! see you next time.")
    finally:
        if durable_session is not None:
            durable_session.close()


if __name__ == "__main__":
    app()
