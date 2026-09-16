from importlib.resources import files
from rich.console import Console

console = Console()

def load_banner() -> str:
    return (
        files("cairn.resources")
        .joinpath("banner.txt")
        .read_text('utf-8')
    )

def print_banner() -> None:
    console.print(load_banner())