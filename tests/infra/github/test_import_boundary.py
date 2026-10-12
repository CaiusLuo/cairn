"""The GitHub package root must stay free of the networking boundary."""

import subprocess
import sys

import pytest

#: The network boundary and its transport, none of which a model import needs.
_NETWORK_MODULES = (
    "http.client",
    "urllib.request",
    "urllib.error",
    "cairn.infra.github.http",
    "cairn.infra.github.source",
    "cairn.infra.github.transport",
)

_SCRIPT = (
    "import importlib, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "forbidden = sorted(\n"
    f"    name for name in {_NETWORK_MODULES!r} if name in sys.modules\n"
    ")\n"
    "print(','.join(forbidden))\n"
    "raise SystemExit(1 if forbidden else 0)\n"
)


@pytest.mark.parametrize("module", ["cairn.infra.github", "cairn.infra.github.models"])
def test_importing_models_does_not_load_the_network_boundary(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, module],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing {module} loaded networking modules: {result.stdout.strip()}\n"
        f"{result.stderr}"
    )
