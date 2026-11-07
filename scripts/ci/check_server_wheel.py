"""Install a wheel outside the checkout and verify bundled Web + API startup."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    wheel = parser.parse_args().wheel.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the isolated wheel check")
    with tempfile.TemporaryDirectory(prefix="cairn-wheel-") as directory:
        root = Path(directory)
        env_root = root / "venv"
        python = env_root / "bin/python"
        subprocess.run(
            [uv, "venv", "--python", sys.executable, str(env_root)],
            check=True,
            timeout=60,
        )
        constraints = root / "constraints.txt"
        constraints.write_text(
            "\n".join(
                name + "==" + distribution.version
                for distribution in metadata.distributions()
                if (name := distribution.metadata.get("Name"))
                and name.lower() != "cairn"
            )
            + "\n"
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--constraint",
                str(constraints),
                "--python",
                str(python),
                str(wheel),
            ],
            check=True,
            timeout=180,
        )
        probe = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from cairn.web.assets import default_web_root, require_web_assets; print(require_web_assets(default_web_root()))",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert Path(probe.stdout.strip()).is_relative_to(env_root), probe.stdout
        source = root / "workspace"
        source.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = root / "config.toml"
        data = root / "data"
        config.write_text(
            "[server]\nworkspace = "
            + json.dumps(str(source))
            + "\ndata_root = "
            + json.dumps(str(data))
            + "\noffline = true\nport = "
            + str(port)
            + "\n"
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CAIRN_") and key != "PYTHONPATH"
        }
        with (root / "server.log").open("w") as output:
            process = subprocess.Popen(
                [str(env_root / "bin/cairn"), "--config", str(config)],
                cwd=root,
                env=environment,
                stdout=output,
                stderr=output,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                with httpx.Client(base_url=url, trust_env=False, timeout=1) as api:
                    until = time.monotonic() + 20
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(
                                (root / "server.log").read_text()[-4000:]
                            )
                        try:
                            if api.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > until:
                            raise RuntimeError("Installed wheel did not become ready")
                        time.sleep(0.1)
                    api.headers["Authorization"] = (
                        "Bearer " + (data / "runtime/runtime.token").read_text().strip()
                    )
                    asset_urls: set[str] = set()
                    for page in ("index", "chat", "tasks", "settings", "login"):
                        response = api.get("/" + page + ".html")
                        assert response.status_code == 200, page
                        asset_urls.update(
                            url
                            for url in re.findall(
                                r'(?:src|href)="([^"]+)"', response.text
                            )
                            if url.startswith("/assets/")
                        )
                    assert asset_urls
                    for asset in asset_urls:
                        assert api.get(asset).status_code == 200, asset
                    assert api.get("/api/runtime/tasks").json() == []
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            assert process.returncode in {0, -signal.SIGTERM}, (
                root / "server.log"
            ).read_text()[-4000:]
            assert "Application shutdown complete" in (root / "server.log").read_text()
    print(
        "Installed wheel verified: five Web pages, referenced assets, shared API and clean shutdown"
    )


if __name__ == "__main__":
    main()
