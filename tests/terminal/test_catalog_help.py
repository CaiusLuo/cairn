from __future__ import annotations

from cairn.interfaces.cli.commands.catalog import CATALOG
from cairn.interfaces.cli.commands.help import handle_help


def test_catalog_resolves_root_and_nested_commands() -> None:
    assert CATALOG.find(("wx",)) is not None
    entry = CATALOG.find(("wx", "disconnect"))
    assert entry is not None
    assert "clear" in entry.summary


def test_help_prints_subcommands_for_every_nested_command(capsys) -> None:  # type: ignore[no-untyped-def]
    for root in ("wx", "schedule", "memory", "model", "provider"):
        handle_help([root])
        output = capsys.readouterr().out
        assert "Subcommands:" in output
        assert root in output


def test_help_prints_a_nested_command(capsys) -> None:  # type: ignore[no-untyped-def]
    handle_help(["wx", "disconnect"])
    output = capsys.readouterr().out
    assert "Usage: disconnect" in output
    assert "fresh scan" in output


def test_router_handles_help_flag_for_all_commands(capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from cairn.agent.tracing.storage import TraceStore
    from cairn.interfaces.cli.commands.context import CommandContext
    from cairn.interfaces.cli.commands.router import CommandRouter

    router = CommandRouter()
    context = CommandContext(trace_store=TraceStore(tmp_path))
    for command in ("/wx", "/schedule", "/memory", "/model", "/provider"):
        result = router.handle(f"{command} --help", context)
        assert result.handled
        assert "Usage:" in capsys.readouterr().out


def test_trace_has_secondary_commands_in_palette_and_help(capsys) -> None:  # type: ignore[no-untyped-def]
    from cairn.interfaces.cli.palette import Palette

    palette = Palette()
    palette.update("/trace ")
    assert [row.name for row in palette.rows()] == ["list", "count", "del"]

    handle_help(["trace"])
    output = capsys.readouterr().out
    assert "Subcommands:" in output
    assert "list" in output and "count" in output and "del" in output
