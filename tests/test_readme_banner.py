"""Keep the README banner identical to the banner the CLI prints."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CLI_BANNER = ROOT / "src" / "cairn" / "resources" / "banner.txt"


def _banner_lines() -> list[str]:
    return CLI_BANNER.read_text(encoding="utf-8").splitlines()


def _wordmark_lines() -> list[str]:
    """The art above the blank separator; the tagline below it is CLI-only."""
    lines = _banner_lines()
    separator = lines.index("") if "" in lines else len(lines)
    return lines[:separator]


def _marked_block(marker: str) -> list[str]:
    document = README.read_text(encoding="utf-8")
    start = f"<!-- {marker}:start -->"
    end = f"<!-- {marker}:end -->"
    assert start in document, f"README.md is missing {start}"
    assert end in document, f"README.md is missing {end}"
    return document.split(start, 1)[1].split(end, 1)[0].splitlines()


def _fenced_lines(block: list[str]) -> list[str]:
    opening = next(index for index, line in enumerate(block) if line.startswith("```"))
    closing = next(
        index
        for index in range(opening + 1, len(block))
        if block[index].startswith("```")
    )
    return block[opening + 1 : closing]


def test_readme_title_matches_the_cli_banner_wordmark() -> None:
    wordmark = _fenced_lines(_marked_block("banner"))

    assert wordmark == _wordmark_lines()


def test_readme_banner_has_no_control_characters() -> None:
    # Escapes or control bytes would render as garbage on GitHub.
    for line in _marked_block("banner"):
        assert all(character.isprintable() or character == "\t" for character in line)
