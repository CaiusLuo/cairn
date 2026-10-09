import sys
from collections.abc import Sequence
from importlib.resources import files

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt
from rich.text import Text

from cairn.core.events import Event
from cairn.core.permissions import PermissionChoice, PermissionRequest
from cairn.llm.model_manager import ModelConfig
from cairn.llm.provider_catalog import NamedProvider

console = Console()


def confirm_provider_access(
    provider: NamedProvider,
    group: ModelConfig,
    *,
    switching: bool,
) -> bool:
    """Approve one provider and model group for this session only.

    All routing, the credential variable name and the transfer notice are shown
    before any credential is resolved. Without an interactive terminal, or on
    end of input, approval fails closed.
    """
    if not sys.stdin.isatty():
        raise ValueError("Provider approval requires an interactive terminal.")
    console.print("Project model configuration requests access to a credential.")
    console.print(f"Provider: {provider.name!a}", markup=False)
    console.print(f"Endpoint: {provider.config.base_url!a}", markup=False)
    console.print(f"Credential variable: {provider.config.api_key_env!a}", markup=False)
    console.print(f"Model group: {group.name!a}", markup=False)
    for position, model_id in enumerate(group.model_ids, start=1):
        console.print(f"  {position}. {model_id!a}", markup=False)
    console.print(
        "The selected API key and conversation content will be sent to this endpoint."
    )
    if switching:
        console.print(
            "Switching providers transfers the full conversation history to that "
            "endpoint and resets session tool-permission grants."
        )
    try:
        return Confirm.ask(
            "Trust this provider for this session?", default=False, console=console
        )
    except EOFError:
        return False


def choose_provider(providers: Sequence[NamedProvider]) -> NamedProvider:
    """Ask which configured provider this session should use.

    Selection is explicit: there is no default, so the user must name one of the
    configured providers. Without an interactive terminal this fails closed.
    """
    if not sys.stdin.isatty():
        raise ValueError("Provider selection requires an interactive terminal.")
    console.print("Several providers are configured; select one for this session:")
    for provider in providers:
        console.print(
            f"  {provider.name}: {provider.config.base_url!a} "
            f"(credential {provider.config.api_key_env!a})",
            markup=False,
        )
    try:
        name = Prompt.ask(
            "Provider",
            choices=[provider.name for provider in providers],
            console=console,
        )
    except EOFError:
        raise ValueError("Provider selection was cancelled.") from None
    return next(provider for provider in providers if provider.name == name)


def choose_model_group(provider: NamedProvider) -> ModelConfig:
    """Ask which of a provider's model groups this session should start on."""
    if not sys.stdin.isatty():
        raise ValueError("Model group selection requires an interactive terminal.")
    groups = provider.config.model_config
    console.print(f"Provider {provider.name!a} configures several model groups:")
    for group in groups:
        console.print(f"  {group.name}: {', '.join(group.model_ids)}", markup=False)
    try:
        name = Prompt.ask(
            "Model group",
            choices=[group.name for group in groups],
            console=console,
        )
    except EOFError:
        raise ValueError("Model group selection was cancelled.") from None
    return next(group for group in groups if group.name == name)


def print_provider_selection(provider: NamedProvider, group: ModelConfig) -> None:
    """Show the provider and model group this session starts with."""
    console.print(f"Provider: {provider.name!a}", markup=False)
    console.print(
        f"Model group: {group.name!a} ({' -> '.join(group.model_ids)})", markup=False
    )


def print_assistant_response(content: str) -> None:
    console.print("[bold]Cairn>[/bold]")
    console.print(Markdown(content))
    console.print()


def print_banner() -> None:
    console.print(files("cairn.resources").joinpath("banner.txt").read_text("utf-8"))


def _render_tool_call(event: Event) -> None:
    raw_tool = event.data.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) else "tool"
    raw_arguments = event.data.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    if tool == "read_file":
        path = arguments.get("path")
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line")
        path_suffix = f" {path}" if isinstance(path, str) else ""
        if end is None or not path_suffix:
            summary = f"→ read_file{path_suffix}"
        else:
            summary = f"→ read_file{path_suffix} lines {start}-{end}"
    elif tool == "edit_file":
        path = arguments.get("path")
        path_suffix = f" {path}" if isinstance(path, str) else ""
        summary = f"→ edit_file{path_suffix}"
    elif tool == "bash":
        command = arguments.get("command")
        command_suffix = f": {command}" if isinstance(command, str) else ""
        summary = f"→ bash{command_suffix}"
    else:
        summary = f"→ {tool}"

    console.print(Text(f"\n{summary}", style="bold cyan"))


def _render_tool_result(event: Event) -> None:
    raw_tool = event.data.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) else "tool"
    exit_code = event.data["exit_code"]
    raw_stdout = event.data.get("stdout")
    stdout = raw_stdout if isinstance(raw_stdout, str) else ""
    raw_stderr = event.data.get("stderr")
    stderr = raw_stderr if isinstance(raw_stderr, str) else ""

    if exit_code != 0:
        console.print(f"✗ exit {exit_code}", style="bold red", markup=False)
        diagnostic = stderr or stdout
        if diagnostic:
            console.print(diagnostic.rstrip(), style="yellow", markup=False)
        return

    if tool == "read_file":
        summary = "✓ read_file"
    elif tool == "edit_file":
        detail = stdout.strip()
        summary = f"✓ {detail}" if detail else "✓ edit_file"
    elif tool == "bash":
        summary = f"✓ exit {exit_code}"
    else:
        summary = f"✓ {tool}"

    console.print(summary, style="green", markup=False)


def _format_token_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "unknown"


def _usage_suffix(event: Event) -> str:
    usage = event.data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    raw_input_tokens = usage.get("input_tokens")
    raw_output_tokens = usage.get("output_tokens")
    input_tokens = _format_token_count(raw_input_tokens)
    output_tokens = _format_token_count(raw_output_tokens)
    return f" · tokens: input {input_tokens}, output {output_tokens}"


def console_event_handler(event: Event) -> None:
    match event.type:
        case "agent_step":
            step = event.data.get("step")
            max_steps = event.data.get("max_steps")

            console.print(f"[dim]step {step}/{max_steps}[/dim]")

        case "context_trimmed":
            omitted_turns = event.data["omitted_turns"]
            omitted_messages = event.data["omitted_messages"]
            console.print(
                f"context: omitted {omitted_turns} older turns "
                f"({omitted_messages} messages); full history retained.",
                style="dim",
                markup=False,
            )

        case "tool_call":
            _render_tool_call(event)

        case "tool_result":
            _render_tool_result(event)

        case "tool_error":
            tool = event.data["tool"]
            error = event.data["error"]

            console.print(f"✗ {tool}: {error}", style="bold red", markup=False)

        case "tool_denied":
            console.print(
                f"✗ denied: {event.data['error']}", style="bold red", markup=False
            )

        case "agent_finish":
            console.print("[dim]✓ done[/dim]")

        case "agent_budget_exhausted":
            reason = event.data["reason"]
            limit = event.data["limit"]
            used = event.data["used"]

            if reason == "max_steps":
                console.print(
                    f"[bold yellow]"
                    f"Agent stopped: step budget exhausted "
                    f"({used}/{limit})."
                    f"[/bold yellow]"
                )

        case "trace_finish":
            trace_id = event.data["trace_id"]
            status = event.data["status"]
            persisted = event.data.get("persisted", True)
            usage_suffix = _usage_suffix(event)

            if persisted:
                console.print(f"[dim]trace: {trace_id} ({status}){usage_suffix}[/dim]")
            else:
                error = event.data.get("persistence_error")
                console.print(
                    f"[yellow]trace unavailable: persistence failed ({error})"
                    f"{usage_suffix}[/yellow]"
                )


def console_permission_prompt(request: PermissionRequest) -> PermissionChoice:
    console.print("\n[bold yellow]Permission required[/bold yellow]\n")
    console.print(f"Capability: {request.capability.value}", markup=False)

    command = request.tool_call.arguments.get("command")
    if isinstance(command, str):
        console.print(f"Command: {command}", markup=False)

    console.print(f"Reason: {request.justification}\n", markup=False)
    console.print(
        "[1] Allow once\n"
        f"[2] Allow {request.capability.value} for this session\n"
        "[3] Deny",
        markup=False,
    )
    choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="3", console=console)
    return {
        "1": PermissionChoice.ALLOW_ONCE,
        "2": PermissionChoice.ALLOW_SESSION,
        "3": PermissionChoice.DENY,
    }[choice]


def print_runtime_error(exc: Exception) -> None:
    console.print(f"✗ {type(exc).__name__}: {exc}", style="bold red", markup=False)
