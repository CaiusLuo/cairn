import asyncio
import os
from pathlib import Path

import typer
from dotenv import dotenv_values

from cairn.assembly import build_agent
from cairn.config import CAIRN_CONFIG_ENV_NAMES as CAIRN_CONFIG_ENV_NAMES
from cairn.config import resolve_cairn_config as resolve_cairn_config
from cairn.config import resolve_context_budget as resolve_context_budget
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBuilder
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.permissions import SessionPermissionHandler
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.storage import TraceStore
from cairn.observability.tracer import Tracer
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
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
    from cairn.llm.token_counter import LiteLLMTokenCounter

    print_banner()

    workspace = Workspace(Path.cwd())
    trace_root = Path(".cairn/traces")
    tracer = Tracer(JsonlTraceSink(trace_root))
    command_context = CommandContext(trace_store=TraceStore(trace_root))
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
        context_builder=ContextBuilder(
            budget=context_budget,
            # Count with LiteLLM's tokenizer for the configured model so the
            # budget reflects the request that is actually sent, not a byte-size
            # heuristic that underestimates code, hashes and base64 output.
            counter=LiteLLMTokenCounter(model),
        ),
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

        if not user_input or user_input.isspace():
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
