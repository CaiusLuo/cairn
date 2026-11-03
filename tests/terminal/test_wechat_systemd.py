import shutil
import subprocess
from pathlib import Path

import pytest


def test_systemd_unit_verifies(tmp_path: Path) -> None:
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is not available")
    template = Path("deploy/systemd/cairn-wechat.service.in").read_text()
    values = {
        "__CAIRN_ROOT__": str(Path.cwd()),
        "__CAIRN_USER__": "root",
        "__CAIRN_HOME__": "/root",
        "__CAIRN_XDG_STATE_HOME__": "/root/.local/state",
        "__WECHAT_SENDER__": "owner",
        "__PROVIDER_NAME__": "provider",
        "__PROVIDER_ENDPOINT__": "https://example.invalid/v1",
        "__PROVIDER_GROUP__": "group",
        "__PROVIDER_KEY_ENV__": "KEY_ENV",
        "__PROVIDER_MODELS__": "model",
    }
    for key, value in values.items():
        template = template.replace(key, value)
    unit = tmp_path / "cairn-wechat.service"
    unit.write_text(template)
    result = subprocess.run(
        [systemd_analyze, "verify", str(unit)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
