import asyncio
import os
from collections.abc import Mapping
from pathlib import Path

import typer
from dotenv import dotenv_values

from cairn.assembly import build_agent
from cairn.commands.context import CommandContext
from cairn.commands.router import CommandRouter
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.permissions import SessionPermissionHandler
from cairn.input import CliInput
from cairn.observability.reader import JsonlTraceReader
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.ui import (
    console_event_handler,
    console_permission_prompt,
    print_assistant_response,
    print_banner,
    print_runtime_error,
)
from cairn.workspace.workspace import Workspace

app = typer.Typer(
    invoke_without_command=True,
)

DEFAULT_CLI_RUN_BUDGET = RunBudget(max_steps=50)

CAIRN_CONFIG_ENV_NAMES = ("CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY", "CAIRN_BASE_URL")


def resolve_cairn_config(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> dict[str, str]:
    """Resolve Cairn configuration from the host environment and ``.env``.

    Host environment values take precedence over the project's ``.env`` file,
    matching the previous ``load_dotenv(override=False)`` behaviour. Unlike
    ``load_dotenv``, this never writes to ``os.environ``, so arbitrary ``.env``
    entries cannot leak into child command environments.
    """
    config: dict[str, str] = {}
    for name in CAIRN_CONFIG_ENV_NAMES:
        value = host_env.get(name, env_file_values.get(name))
        if not value:
            raise ValueError(f"{name} environment variable is not set.")
        config[name] = value
    return config


def resolve_context_budget(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> ContextBudget:
    defaults = ContextBudget()

    def integer_setting(name: str, default: int) -> int:
        value = host_env.get(name, env_file_values.get(name))
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer.") from None

    return ContextBudget(
        max_tokens=integer_setting("CAIRN_CONTEXT_MAX_TOKENS", defaults.max_tokens),
        response_tokens=integer_setting(
            "CAIRN_RESPONSE_MAX_TOKENS", defaults.response_tokens
        ),
    )


@app.callback()
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(main())


async def main(cli_input: CliInput | None = None) -> None:
    env_file_values = dotenv_values()
    config = resolve_cairn_config(os.environ, env_file_values)
    context_budget = resolve_context_budget(os.environ, env_file_values)
    model = config["CAIRN_LLM_MODEL"]
    api_key = config["CAIRN_LLM_API_KEY"]
    base_url = config["CAIRN_BASE_URL"]

    from cairn.llm.litellm_client import LiteLLMClient

    print_banner()

    workspace = Workspace(Path.cwd())
    trace_root = Path(".cairn/traces")
    tracer = Tracer(JsonlTraceSink(trace_root))
    command_context = CommandContext(trace_reader=JsonlTraceReader(trace_root))
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
        llm=LiteLLMClient(
            model=model,
            api_key=api_key,
            api_base=base_url,
            max_output_tokens=context_budget.response_tokens,
        ),
        event_handler=handle_event,
        permission_handler=SessionPermissionHandler(prompt=console_permission_prompt),
        tracer=tracer,
        context_builder=ContextBuilder(budget=context_budget),
    )

    router = CommandRouter()
    input_reader = cli_input or CliInput()

    while True:
        try:
            user_input = await input_reader.read()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break

        if not user_input.strip():
            continue

        command_result = router.handle(
            user_input,
            command_context,
        )

        if command_result.handled:
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
