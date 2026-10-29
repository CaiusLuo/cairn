import asyncio
import contextlib
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget
from cairn.core.events import Event, EventHandler
from cairn.core.model_manager import ModelConfig
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog
from cairn.infra.config import (
    CAIRN_MAX_STEPS_ENV_NAME,
    ConfigLayout,
    ProjectProviders,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.observability.otlp import OtlpSpanExporter, build_exporter
from cairn.infra.observability.sinks import FanoutTraceSink, JsonlTraceSink
from cairn.infra.observability.storage import TraceStore
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.skills import builtin_skills
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.assembly import build_agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.lifecycle import await_owned, settle_owned
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
from cairn.terminal.action import Action, ActionRow, Tone, consent, note, refusal
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.model import model_chooser_at
from cairn.terminal.commands.provider import provider_chooser_at
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
from cairn.terminal.headless import task_app
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
    print_turn_separator,
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
app.add_typer(task_app, name="task")
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
    streaming: StreamingDisplay,
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
        streaming=streaming,
        durable_session=durable_session,
    )


async def _drive_turn(
    *,
    agent: Agent,
    user_input: str,
    steering: SteerQueue,
    frame: TranscriptView,
    streaming: StreamingDisplay,
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

    #: Set by the key binding immediately before it cancels the turn. Cancellation
    #: reaches a task as a bare ``CancelledError`` that cannot say who asked, so this
    #: flag is the only evidence that the stop was the operator's — the difference
    #: between "the turn stopped because I asked" and "the turn failed".
    operator_stopped = False

    def request_stop() -> None:
        """Stop the running turn, saying so while the cleanup completes."""
        nonlocal operator_stopped
        if operator_stopped:
            # A second press must not re-request a cancellation already in flight.
            return
        operator_stopped = True
        streaming.begin_interrupt()
        turn.cancel()

    previous_interrupt = frame.on_interrupt
    frame.on_interrupt = request_stop
    try:
        while True:
            done, _ = await asyncio.wait(
                {turn, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if turn in done:
                if operator_stopped and turn.cancelled():
                    # The operator asked for this, so the surface reports it and the
                    # turn loop carries on to the next line. The outcome is a
                    # dedicated exception rather than a sentinel value, so it cannot
                    # be confused with a real answer.
                    streaming.settle_interrupt()
                    raise TurnStopped
                # Anything else is a real failure the turn loop must report, and a
                # cancellation nobody asked for is reported as one too.
                if turn.cancelled():
                    raise asyncio.CancelledError(
                        "the turn was cancelled without an operator request"
                    )
                return turn.result()
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
        # The binding belongs to this turn: restoring it means a later Ctrl-C cannot
        # reach a turn that has already finished.
        frame.on_interrupt = previous_interrupt
        await settle_owned(turn, reader)


def _terminal_width() -> int | None:
    """The terminal's columns, when there is a terminal to ask.

    Off terminal there is no width to fit, and the status keeps its full form: a
    log has no wrapping to avoid.
    """
    try:
        return shutil.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        return None


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


class TurnStopped(Exception):
    """Raised inside a turn whose cancellation the operator asked for.

    Cancellation reaches an ``asyncio`` task as a bare ``CancelledError``, which
    cannot say *who* asked. The key binding records that it requested the stop before
    cancelling, so this exception carries the answer out to the turn loop: an
    operator's own Ctrl-C is an outcome the session continues from, while anything
    else that cancels the turn is a real failure the loop must report.
    """


#: The row that grants. Naming it once keeps the surface and the reader of the
#: answer in agreement about what consent looks like.
TRUST_ROW = "Trust for this session"


def _trust_decision(provider: NamedProvider, group: ModelConfig) -> Action:
    """The consent surface for one provider switch.

    Every fact the operator needs is stated in full: where the conversation is sent,
    which credential *variable* will be read, and which models it covers. A shortened
    endpoint would hide exactly the thing a lookalike host relies on, so it is shown
    whole and the surface wraps it.
    """
    return consent(
        "\n".join(
            [
                f"Send this session's conversation to {provider.name}:",
                f"  {provider.config.base_url}",
                f"Credential variable  {provider.config.api_key_env}",
                f"Models              {', '.join(group.model_ids)}",
                "The full conversation history moves with it, and session tool "
                "permissions are reset.",
            ]
        ),
        grant=TRUST_ROW,
        decline="Cancel",
    )


PERMISSION_ALLOW_ONCE = "Allow once"
PERMISSION_ALLOW_SESSION = "Allow for this session"
PERMISSION_DENY = "Deny"


def _permission_prompt_for(
    frame: TranscriptView | None,
) -> Callable[[PermissionRequest], Awaitable[PermissionChoice]]:
    """Build an async approval prompt that yields to the live terminal loop."""
    assert frame is not None

    async def ask(request: PermissionRequest) -> PermissionChoice:
        return await _ask_permission_in_surface(request, frame=frame)

    return ask


def _permission_decision(request: PermissionRequest) -> Action:
    """The consent surface for one tool capability, defaulting to Deny.

    The facts are the ones the decision turns on: which capability, the concrete
    command or paths it would act on, and why the tool asked. They are stated in
    full — a path set is exactly what a filesystem grant hands out — and the cursor
    starts on Deny, so Enter on an untouched menu refuses.
    """
    lines = [f"Permission required: {request.capability.value}"]
    command = request.tool_call.arguments.get("command")
    if isinstance(command, str):
        lines.append(f"  Command  {command}")
    for path in request.paths:
        lines.append(f"  Path     {path}")
    if request.justification:
        lines.append(f"  Reason   {request.justification}")
    return Action(
        message="\n".join(lines),
        rows=(
            ActionRow(name=PERMISSION_ALLOW_ONCE),
            ActionRow(name=PERMISSION_ALLOW_SESSION),
            ActionRow(name=PERMISSION_DENY, cancel=True),
        ),
        selected=2,
        hint="Up/Down move   Enter confirm   Esc deny",
        tone=Tone.REFUSAL,
        decides_authority=True,
    )


async def _ask_permission_in_surface(
    request: PermissionRequest, *, frame: TranscriptView
) -> PermissionChoice:
    """Ask for tool authority on the bottom surface and await the answer.

    Every outcome other than an explicit allow is a denial: Escape, end of input and
    a closed surface all deny, because a capability that was never granted must never
    be inferred from a keystroke that did not mean it.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[PermissionChoice] = loop.create_future()

    def take(choice: PermissionChoice) -> None:
        if not answer.done():
            answer.set_result(choice)

    previous_choose = frame.on_action_choose
    previous_command = frame.on_action_command
    previous_move = frame.on_action_move
    action = _permission_decision(request)

    def choose(name: str) -> None:
        take(
            {
                PERMISSION_ALLOW_ONCE: PermissionChoice.ALLOW_ONCE,
                PERMISSION_ALLOW_SESSION: PermissionChoice.ALLOW_SESSION,
            }.get(name, PermissionChoice.DENY)
        )

    def command(name: str) -> None:
        if not name:
            take(PermissionChoice.DENY)

    def move(current: int, step: int) -> Action:
        action.selected = max(0, min(len(action.rows) - 1, current + step))
        frame.show_action(action)
        return action

    frame.on_action_choose = choose
    frame.on_action_command = command
    frame.on_action_move = move
    try:
        frame.show_action(action)
        return await answer
    finally:
        frame.on_action_choose = previous_choose
        frame.on_action_command = previous_command
        frame.on_action_move = previous_move
        frame.show_action(None)


async def _ask_in_surface(
    provider: NamedProvider,
    group: ModelConfig,
    *,
    frame: TranscriptView,
) -> bool:
    """Ask for provider approval on the bottom surface and await the answer.

    The runtime asks this question in the middle of building a replacement binding,
    so the answer has to arrive before that call returns; awaiting it on the same
    loop is what lets the surface's keys resolve while the question is open.

    Every outcome other than an explicit Trust is a refusal: Cancel, Escape, end of
    input, and a surface closed without an answer all resolve to False.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[bool] = loop.create_future()

    def take(choice: bool) -> None:
        if not answer.done():
            answer.set_result(choice)

    previous_choose = frame.on_action_choose
    previous_command = frame.on_action_command
    previous_move = frame.on_action_move
    action = _trust_decision(provider, group)

    def choose(name: str) -> None:
        # Only the grant row grants. Any other name is a refusal, so a renamed or
        # mismatched row can never be read as consent.
        take(name == TRUST_ROW)

    def command(name: str) -> None:
        # An empty command means the surface closed without an answer.
        if not name:
            take(False)

    def move(current: int, step: int) -> Action:
        action.selected = max(0, min(len(action.rows) - 1, current + step))
        frame.show_action(action)
        return action

    frame.on_action_choose = choose
    frame.on_action_command = command
    frame.on_action_move = move
    try:
        frame.show_action(action)
        return await answer
    finally:
        frame.on_action_choose = previous_choose
        frame.on_action_command = previous_command
        frame.on_action_move = previous_move
        frame.show_action(None)


def _confirm_provider(
    provider: NamedProvider,
    group: ModelConfig,
    *,
    frame: TranscriptView | None,
) -> Awaitable[bool]:
    """Resolve provider approval: on the surface when there is one, else the console.

    A synchronous console question cannot be used while the surface is live, because
    it would block the loop the surface is drawn on. Returning an awaitable keeps the
    choice at the caller, which is the only place that knows whether a loop is
    running.
    """
    if frame is None or not _interactive_terminal():
        return asyncio.to_thread(
            lambda: confirm_provider_access(provider, group, switching=True) is True
        )

    async def ask() -> bool:
        return await _ask_in_surface(provider, group, frame=frame)

    return ask()


#: Interactions currently running from a key handler. Kept alive until they finish,
#: because a task nobody references can be collected mid-await.
_scheduled: list[asyncio.Task[None]] = []


def _schedule_interaction(
    interaction: Awaitable[Action | None],
    frame: TranscriptView | None,
    *,
    command: str = "",
) -> None:
    """Run a command's interaction on the loop, then show what it concluded.

    The key handler that produced this is synchronous, so it cannot await. The
    interaction is scheduled instead; dropping it is what left a provider chooser
    looking responsive while no trust question was ever asked.
    """
    loop = asyncio.get_running_loop()

    async def run() -> None:
        try:
            action = await interaction
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            _show_action(refusal(f"{type(exc).__name__}: {exc}"), frame)
            return
        _show_action(action, frame, command=command)

    # The reference is kept so the task is not collected while it is still running,
    # and dropped once it finishes so the list cannot grow for the session's life.
    task = loop.create_task(run())
    _scheduled.append(task)
    task.add_done_callback(
        lambda finished: _scheduled.remove(finished) if finished in _scheduled else None
    )


async def _settle_interactions() -> None:
    """Cancel and observe every key-handler interaction before session teardown."""
    owned = tuple(_scheduled)
    if not owned:
        return
    await settle_owned(*owned)


def _details_for(
    command: str,
    action: Action,
    context: CommandContext,
    *,
    selected: int | None = None,
) -> Action:
    """Rebuild ``action`` with ``selected`` chosen and that row's details expanded."""
    index = action.selected if selected is None else selected
    if command == "/model":
        return model_chooser_at(context, index)
    if command == "/provider":
        return provider_chooser_at(context, index)
    action.selected = index
    return action.clamped()


def _show_action(
    action: Action | None,
    frame: TranscriptView | None,
    *,
    command: str = "",
) -> None:
    """Show an action where the operator will see it.

    In a terminal that is the surface above the composer; in a script there is no
    surface, so the message is printed as plain text. A scripted run must still be
    told what a command did — it just cannot be told it with a menu.

    ``command`` names the command that produced the action, so a later Enter or arrow
    knows which chooser it is answering.
    """
    if action is None:
        return
    if frame is not None:
        frame.show_action(action)
        if frame.on_action_command is not None:
            frame.on_action_command(command if action.rows else "")
        return
    print(action.message)


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
    needs_turn_gap = False
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
            command = user_input.split()[0].lower()
            _show_action(command_result.action, frame, command=command)
            if command_result.interaction is not None:
                _show_action(await command_result.interaction(), frame, command=command)
            if command_result.should_exit:
                break
            continue
        streaming_display.reset()
        if frame is not None:
            frame.set_running(True)
        if needs_turn_gap:
            # One separator belongs between a settled reply/outcome and the next
            # user block. Commands do not set this flag, so their output cannot
            # accumulate extra blank rows.
            print_turn_separator()
            needs_turn_gap = False
        # Echo the prompt before the answer so the transcript keeps both sides of
        # the conversation; the reply alone leaves the reader reconstructing what
        # was asked.
        print_user_message(user_input)
        previous_seq = (
            durable_session.receipt.revision.head_seq if durable_session else None
        )
        # A single flag records whether the turn reported its own outcome, so the
        # cleanup path cannot swallow an ordinary failure or a budget stop.
        settled = False
        try:
            response = await _run_supervised_turn(
                agent=agent,
                user_input=user_input,
                frame=frame,
                input_reader=input_reader,
                streaming=streaming_display,
                durable_session=durable_session,
            )
        except TurnStopped:
            # `settle_interrupt` already replaced the partial attempt with the
            # receipt; nothing more belongs in the transcript.
            settled = True
        except RunBudgetExceeded:
            # The budget guard has already said what stopped the turn.
            settled = True
        except asyncio.CancelledError:
            # A cancellation nobody requested: the session is being torn down. The
            # turn cannot finish, so it is reported and propagates after cleanup.
            settled = True
            raise
        except Exception as exc:
            settled = True
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
            # The display drops any half-written block in every outcome, but only
            # speaks up when the turn did not already say what happened: an
            # interrupt reported itself through `settle_interrupt`, and a failure was
            # reported above, so the generic warning would repeat the same fact.
            streaming_display.close_incomplete(silent=settled)
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
        needs_turn_gap = True


def _build_trace_sink(
    trace_root: Path, env_file_values: dict[str, str | None] | None
) -> tuple[OtlpSpanExporter | None, JsonlTraceSink | FanoutTraceSink]:
    """Build the local JSONL sink plus an optional opt-in OTLP exporter.

    The JSONL sink is always the local record. The exporter rides beside it via a
    fan-out so a remote failure never stops the local trace. Returns the exporter
    (to be closed on shutdown) and the sink.
    """
    otlp_env = {
        key: value
        for key, value in (env_file_values or {}).items()
        if value is not None
    }
    exporter = build_exporter({**otlp_env, **os.environ})
    sink = (
        FanoutTraceSink(JsonlTraceSink(trace_root), exporter)
        if exporter is not None
        else JsonlTraceSink(trace_root)
    )
    return exporter, sink


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
    exporter, trace_sink = _build_trace_sink(trace_root, env_file_values)
    tracer = Tracer(trace_sink)
    command_context = CommandContext(trace_store=TraceStore(trace_root))
    # The prompt is bound once the frame exists, because the surface is where an
    # approval is given. Until then the console prompt is the only option, and it is
    # what the startup bootstrap uses.
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
    # With a surface to ask on, tool approvals go there: the console prompt would
    # block the loop the surface is drawn on, so it could never be answered. Without
    # one, the handler keeps the prompt it was built with.
    if frame is not None:
        permission_handler.prompt_async = _permission_prompt_for(frame)
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
    # The status must fit the terminal it is printed in; the frame reports the width
    # it actually has, so a narrow window shortens rather than wraps.
    streaming_display.set_status_width(
        frame.available_width() if frame is not None else _terminal_width()
    )
    if frame is not None:
        # Ctrl+O reveals more reasoning. A keystroke rather than a click: capturing
        # the mouse would cost the native drag-select and copy this layout exists to
        # keep.
        frame.on_reveal_reasoning = streaming_display.reveal_reasoning
        # A resize changes how much room the two status rows have. The frame reports
        # the width whenever it changes, so the display re-fits instead of keeping
        # the width it started with and overflowing a narrower terminal.
        frame.on_width = streaming_display.set_status_width
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

    def _publish_identity(provider_name: str) -> None:
        """Tell the status which model and provider the session now talks to."""
        manager = command_context.model_manager
        model = manager.current_model().model_ids[0] if manager is not None else ""
        streaming_display.observe_model(model, provider=provider_name)

    def mirror_committed_runtime(runtime: ProviderRuntime) -> None:
        """Keep the command context pointing at the runtime runtime owns."""
        command_context.model_manager = runtime.model_manager
        command_context.model_executor = runtime.model_executor
        command_context.active_provider_name = runtime.provider.name
        _publish_identity(runtime.provider.name)

    # The runtime owns which provider and model are active and every rebinding a
    # switch performs; the CLI keeps prompting, printing and credential lookup.
    session = ProviderSession(
        active_runtime,
        agent=agent,
        on_commit=mirror_committed_runtime,
    )

    #: The provider the operator has just approved on the surface, for the switch
    #: currently being performed. The runtime asks ``confirm`` synchronously inside
    #: its build-before-commit sequence, so the question is asked and answered
    #: *before* that call: by the time the runtime asks, the answer is known.
    approved_provider: str | None = None

    def select_provider(name: str) -> None:
        """Switch providers, using the trust decision already taken.

        The runtime keeps build-before-commit: it asks for approval, resolves only
        the approved provider's credential, builds the replacement and commits it.
        The question itself was asked on the surface, because a synchronous console
        prompt would block the loop the surface is drawn on.
        """
        nonlocal approved_provider

        def confirm(provider: NamedProvider, group: ModelConfig) -> bool:
            # Consent is bound to this exact provider. A different target than the
            # one the operator approved is not consent, however the call arrived.
            return approved_provider == provider.name

        switched = session.select_provider(
            name,
            catalog,
            confirm=confirm,
            resolve_credential=lambda config: resolve_provider_api_key(
                config, os.environ, env_file_values
            ),
            context_budget=context_budget,
        )
        if switched:
            permission_handler.reset_grants()
            # The footer names what the session is talking to, so a committed switch
            # has to reach it: leaving the startup identity there would describe a
            # provider the session no longer uses.
            _publish_identity(name)

    def _provider_summary_action(name: str) -> Action:
        """One line confirming what the committed switch left the session on."""
        return note(f"Current provider: {name}")

    async def approve_provider(name: str) -> Action:
        """Ask for trust on the surface, then perform the switch.

        Approval and switching are one step: the answer is stored, the runtime is
        asked to switch, and the surface reports what happened. A refusal never
        reaches the runtime, so no credential is resolved for a provider the
        operator declined.
        """
        nonlocal approved_provider
        target = catalog.by_name(name)
        if target is None:
            return refusal(f"Provider {name!r} is not configured in this session.")
        if target.name == (command_context.active_provider_name or target.name):
            return note(f"Current provider: {name}")
        group = target.config.model_config[0]
        granted = False
        if frame is not None and _interactive_terminal():
            granted = await _ask_in_surface(target, group, frame=frame)
        else:
            granted = (
                await asyncio.to_thread(
                    lambda: confirm_provider_access(target, group, switching=True)
                )
                is True
            )
        if not granted:
            return refusal(f"Provider {name!r} was not approved for this session.")
        approved_provider = target.name
        try:
            select_provider(target.name)
        except ValueError as exc:
            return refusal(str(exc))
        finally:
            approved_provider = None
        return _provider_summary_action(name)

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
    # Provider trust is a decision, so the command returns it as an interaction and
    # the operator answers it on the surface with the loop still drawing.
    command_context.approve_provider = approve_provider
    command_context.provider_catalog = catalog
    # Commands run between turns only, so a switch can never race a turn.

    router = CommandRouter()
    input_reader = cli_input or CliInput()
    # The frame's bottom surface is the chooser, so the legacy separate picker is
    # not wired: two places to make the same decision is one place too many.
    if frame is not None:
        command_context.has_action_surface = True

        # What a chooser row means is the command that opens it. Keeping this here,
        # rather than on the frame, is what lets one surface serve both commands
        # without the frame knowing either of them.
        chooser: dict[str, str] = {"command": ""}

        def choose_row(name: str) -> None:
            """Confirm the row the operator picked.

            A model row means ``/model use <name>``, so the menu and the typed form
            share one path through the runtime and its build-before-commit rules.

            A provider row means a trust question first. That question is returned as
            an interaction rather than answered inside the command, because the
            operator has to answer it on this surface — so the interaction is
            scheduled on the loop instead of being dropped.
            """
            command = chooser["command"]
            if command == "/model":
                result = router.handle(f"/model use {name}", command_context)
            elif command == "/provider":
                result = router.handle(f"/provider use {name}", command_context)
                if result.interaction is not None:
                    _schedule_interaction(result.interaction(), frame, command=command)
                    return
            else:
                return
            _show_action(result.action, frame)

        def move_row(current: int, step: int) -> Action | None:
            """Re-expand the chosen row's details after the selection moved.

            A chooser shows one entry's facts at a time, so moving the cursor has to
            rebuild the message. Re-running the command is what keeps those facts
            real rather than a copy taken when the menu opened.

            The move starts from ``current``, the row on screen. Re-running the
            command builds an action whose selection is the first row, so adding
            ``step`` to *that* index would stop repeated Down at the second entry and
            send Up back to the first.
            """
            command = chooser["command"]
            if not command:
                return None
            result = router.handle(command, command_context)
            rebuilt = result.action
            if rebuilt is None or not rebuilt.rows:
                return None
            target = max(0, min(len(rebuilt.rows) - 1, current + step))
            return _details_for(command, rebuilt, command_context, selected=target)

        frame.on_action_choose = choose_row
        frame.on_action_move = move_row
        # Remember which command opened the chooser, so a later Enter knows which
        # one it is answering. Without this the row names a group but no command
        # ever consumes it.
        frame.on_action_command = lambda name: chooser.__setitem__("command", name)

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
    primary_error: BaseException | None = None
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
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            await _settle_interactions()
        except BaseException as exc:
            cleanup_error = exc
        if frame is not None:
            try:
                closing = asyncio.create_task(frame.aclose())
                cancellation = await await_owned(closing)
                closing.result()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note("Frame cleanup also failed.")
        if durable_session is not None:
            try:
                durable_session.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note("Durable session cleanup also failed.")
        if exporter is not None:
            # Bounded flush of queued spans; a failed export is telemetry loss,
            # never a terminal cleanup failure.
            with contextlib.suppress(BaseException):
                exporter.close()
        if primary_error is not None:
            if cleanup_error is not None:
                primary_error.add_note("Terminal cleanup also failed.")
            if cancellation is not None:
                primary_error.add_note("Terminal cleanup completed after cancellation.")
        elif cleanup_error is not None:
            raise cleanup_error
        elif cancellation is not None:
            raise cancellation


if __name__ == "__main__":
    app()
