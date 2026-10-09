import asyncio
import os
from pathlib import Path

import typer
from dotenv import dotenv_values

from cairn.assembly import build_agent
from cairn.config import CAIRN_CONFIG_ENV_NAMES as CAIRN_CONFIG_ENV_NAMES
from cairn.config import (
    load_model_config,
    resolve_provider_api_key,
    validate_runtime_provider,
)
from cairn.config import resolve_cairn_config as resolve_cairn_config
from cairn.config import resolve_context_budget as resolve_context_budget
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBuilder
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.permissions import SessionPermissionHandler
from cairn.llm.model_executor import ModelExecutor
from cairn.llm.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.storage import TraceStore
from cairn.observability.tracer import Tracer
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
    confirm_model_provider,
    console_event_handler,
    console_permission_prompt,
    print_assistant_response,
    print_banner,
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
    try:
        model_path.lstat()
    except FileNotFoundError:
        config = resolve_cairn_config(os.environ, env_file_values)
        provider = ProviderConfig(
            base_url=config["CAIRN_BASE_URL"],
            api_key_env="CAIRN_LLM_API_KEY",
            model_config=(
                ModelConfig(
                    name=config["CAIRN_LLM_MODEL"],
                    model_ids=(config["CAIRN_LLM_MODEL"],),
                ),
            ),
        )
    else:
        # Existing but unreadable/invalid files (including dangling symlinks)
        # must not silently select a different provider or credential.
        provider = load_model_config(model_path)
        validate_runtime_provider(provider)
        if confirm_model_provider(provider) is not True:
            raise ValueError(
                "Project model provider was not approved for this session."
            )

    api_key = resolve_provider_api_key(provider, os.environ, env_file_values)
    model_manager = ModelManager(provider)
    context_budget = resolve_context_budget(os.environ, env_file_values)

    from cairn.llm.litellm_client import LiteLLMClient
    from cairn.llm.token_counter import LiteLLMTokenCounter

    def create_model_runtime(
        model_id: str,
    ) -> tuple[LiteLLMClient, ContextBuilder]:
        return (
            LiteLLMClient(
                model=model_id,
                api_key=api_key,
                api_base=provider.base_url,
                max_output_tokens=context_budget.response_tokens,
            ),
            ContextBuilder(
                budget=context_budget,
                counter=LiteLLMTokenCounter(model_id),
            ),
        )

    llm, context_builder = create_model_runtime(
        model_manager.current_model().model_ids[0]
    )
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

    model_executor = ModelExecutor(model_manager, create_model_runtime)
    agent = build_agent(
        workspace=workspace,
        llm=llm,
        event_handler=handle_event,
        permission_handler=SessionPermissionHandler(prompt=console_permission_prompt),
        tracer=tracer,
        context_builder=context_builder,
        secret_env_keys=frozenset({provider.api_key_env}),
        model_executor=model_executor,
    )

    def select_model(name: str) -> ModelConfig:
        previous = model_manager.current_model()
        selected = model_manager.select_model(name)
        try:
            next_llm, next_context = create_model_runtime(selected.model_ids[0])
        except Exception:
            model_manager.select_model(previous.name)
            raise
        # The CLI handles commands between turns. No await separates the pair,
        # and the existing Agent (state, tools, permissions and tracer) is kept.
        agent.llm, agent.context_builder = next_llm, next_context
        return selected

    command_context.model_manager = model_manager
    command_context.model_executor = model_executor
    command_context.select_model = select_model
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
