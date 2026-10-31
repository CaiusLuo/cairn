"""``cairn wechat login`` and ``cairn wechat serve``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from cairn.infra.mcp.config import McpServerConfig, load_mcp_servers
from cairn.infra.schedule.store import default_schedule_dir
from cairn.infra.skills import layered_skills, resolve_skills
from cairn.infra.wechat.ilink import ILinkError
from cairn.infra.wechat.state import (
    WechatLockError,
    WechatState,
    default_state_dir,
    ensure_outside_workspace,
)
from cairn.terminal.headless import (
    DEFAULT_CHECK_TIMEOUT_S,
    DEFAULT_MAX_STEPS,
    _build_approval,
)
from cairn.terminal.wechat.login import QRLoginError, run_login
from cairn.terminal.wechat.serve import ServeConfig, serve_wechat

wechat_app = typer.Typer(help="Login to WeChat iLink and serve text coding tasks.")


@wechat_app.command("login")
def wechat_login(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-w", help="Explicit target Workspace.")
    ],
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", help="Host-private state directory."),
    ] = None,
    base_url: Annotated[
        str, typer.Option("--base-url", help="iLink API base URL.")
    ] = "https://ilinkai.weixin.qq.com",
) -> None:
    """Fetch a QR code, drive the scan handshake and save the account (0600)."""
    target = state_dir if state_dir is not None else default_state_dir()
    token_seen = {"value": ""}

    def on_qr(qr) -> None:  # type: ignore[no-untyped-def]
        typer.echo("请使用微信扫描二维码:")
        typer.echo(qr.image_content or qr.qrcode)

    def on_status(status: str) -> None:
        typer.echo(f"登录状态: {status}")

    def prompt_verify(message: str) -> str:
        return str(typer.prompt(message))

    try:
        account = asyncio.run(
            run_login(
                state_dir=target,
                workspace=workspace.resolve(),
                base_url=base_url,
                prompt_verify=prompt_verify,
                on_qr=on_qr,
                on_status=on_status,
            )
        )
    except (QRLoginError, ILinkError, OSError, ValueError) as exc:
        typer.echo(f"登录失败: {exc}", err=True)
        raise typer.Exit(1) from None
    token_seen["value"] = account.bot_token
    typer.echo(f"登录成功: bot={account.ilink_bot_id} state={target}")
    # The token is never printed; callers read it only from the 0600 state file.


@wechat_app.command("serve")
def wechat_serve(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-w", help="Explicit target Workspace.")
    ],
    allow_sender: Annotated[
        list[str] | None,
        typer.Option("--allow-sender", help="Trusted sender id (repeatable)."),
    ] = None,
    trust_workspace: Annotated[
        bool,
        typer.Option("--trust-workspace", help="Explicitly trust the Workspace."),
    ] = False,
    approve_provider_name: Annotated[
        str | None, typer.Option("--approve-provider-name")
    ] = None,
    approve_provider_endpoint: Annotated[
        str | None, typer.Option("--approve-provider-endpoint")
    ] = None,
    approve_provider_group: Annotated[
        str | None, typer.Option("--approve-provider-group")
    ] = None,
    approve_provider_key_env: Annotated[
        str | None, typer.Option("--approve-provider-key-env")
    ] = None,
    approve_provider_models: Annotated[
        str | None, typer.Option("--approve-provider-models")
    ] = None,
    allow_shell: Annotated[bool, typer.Option("--allow-shell")] = False,
    allow_mcp: Annotated[bool, typer.Option("--allow-mcp")] = False,
    allow_network: Annotated[bool, typer.Option("--allow-network")] = False,
    check_command: Annotated[str | None, typer.Option("--check-command")] = None,
    check_timeout: Annotated[
        float, typer.Option("--check-timeout")
    ] = DEFAULT_CHECK_TIMEOUT_S,
    skill: Annotated[list[str] | None, typer.Option("--skill")] = None,
    mcp: Annotated[list[str] | None, typer.Option("--mcp")] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    max_steps: Annotated[int, typer.Option("--max-steps")] = DEFAULT_MAX_STEPS,
    trace_dir: Annotated[Path | None, typer.Option("--trace-dir")] = None,
    poll_timeout_ms: Annotated[int | None, typer.Option("--poll-timeout-ms")] = None,
    poll_interval: Annotated[float, typer.Option("--poll-interval")] = 0.0,
) -> None:
    """Long-poll one WeChat account and run one coding task at a time."""
    if not allow_sender:
        typer.echo("--allow-sender is required; no sender is trusted.", err=True)
        raise typer.Exit(2)
    if not trust_workspace:
        typer.echo("--trust-workspace is required to run tasks.", err=True)
        raise typer.Exit(2)
    resolved_workspace = workspace.resolve()
    if not resolved_workspace.is_dir():
        typer.echo(f"Workspace is not a directory: {resolved_workspace}", err=True)
        raise typer.Exit(2)
    target_state = state_dir if state_dir is not None else default_state_dir()
    try:
        ensure_outside_workspace(target_state, resolved_workspace)
        state = WechatState(target_state)
        account = state.load_account()
    except (OSError, ValueError) as exc:
        typer.echo(f"Invalid WeChat state: {exc}", err=True)
        raise typer.Exit(2) from None
    if account is None:
        typer.echo("No saved WeChat account; run `cairn wechat login` first.", err=True)
        raise typer.Exit(2)
    approval = _build_approval(
        approve_provider_name,
        approve_provider_endpoint,
        approve_provider_group,
        approve_provider_key_env,
        approve_provider_models,
    )
    selected_skills: tuple[str, ...] = ()
    if skill:
        found, missing = resolve_skills(layered_skills(resolved_workspace), skill)
        if missing:
            typer.echo("Unknown skill: " + ", ".join(missing), err=True)
            raise typer.Exit(2)
        selected_skills = tuple(item.name for item in found)
    mcp_configs: tuple[McpServerConfig, ...] = ()
    if mcp:
        try:
            available = load_mcp_servers(resolved_workspace)
        except (OSError, ValueError) as exc:
            typer.echo(f"MCP config error: {exc}", err=True)
            raise typer.Exit(2) from None
        by_name = {server.name: server for server in available}
        missing = [name for name in mcp if name not in by_name]
        if missing:
            typer.echo("Unknown MCP server: " + ", ".join(missing), err=True)
            raise typer.Exit(2)
        selected_servers = tuple(by_name[name] for name in dict.fromkeys(mcp))
        if not allow_mcp:
            typer.echo("--mcp requires --allow-mcp.", err=True)
            raise typer.Exit(2)
        if any(s.transport == "stdio" for s in selected_servers) and not allow_shell:
            typer.echo("stdio MCP requires --allow-shell.", err=True)
            raise typer.Exit(2)
        if (
            any(s.transport == "streamable_http" for s in selected_servers)
            and not allow_network
        ):
            typer.echo("HTTP MCP requires --allow-network.", err=True)
            raise typer.Exit(2)
        mcp_configs = selected_servers
    config = ServeConfig(
        workspace=resolved_workspace,
        state_dir=target_state.resolve(),
        account=account,
        approval=approval,
        allowed_senders=frozenset(allow_sender),
        allow_shell=allow_shell,
        allow_mcp=allow_mcp,
        allow_network=allow_network,
        check_command=check_command,
        check_timeout=check_timeout,
        max_steps=max_steps,
        trace_dir=trace_dir,
        selected_skills=selected_skills,
        mcp_configs=mcp_configs,
        poll_timeout_ms=poll_timeout_ms,
        poll_interval=poll_interval,
        schedule_store=default_schedule_dir(),
    )
    try:
        report = asyncio.run(serve_wechat(config=config, state=state))
    except WechatLockError as exc:
        typer.echo(f"WeChat serve lock: {exc}", err=True)
        raise typer.Exit(3) from None
    except (ILinkError, OSError, ValueError) as exc:
        typer.echo(f"WeChat serve error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        "wechat serve stopped: "
        f"polls={report.polls} messages={report.messages_seen} "
        f"tasks={report.tasks_started} replies={report.replies_sent} "
        f"interrupted={report.interrupted} errors={report.errors} "
        f"scheduled={report.scheduled_jobs} "
        f"scheduled_delivered={report.scheduled_delivered}"
    )
