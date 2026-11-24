# ruff: noqa: RUF001
"""Host-authorized provider configuration from the Web console.

A save edits the same files a restart would read — the provider catalog in
``.cairn/models.toml``, the credential in the workspace ``.env``, the regenerated
allowlist and the durable model selection — then installs the approved catalog
into the running Server. New work uses it immediately; a turn that already
started keeps the runtime it captured. Responses never resolve a credential
value.

Only the provider and model group being edited are changed. Other providers,
other groups, their order and unrelated TOML keys stay in the document. Every
provider in the catalog is approved on save, so any configured model can be
selected without a separate enable step. A selection change is a separate
operation: :meth:`ModelSettingsService.select` updates the runtime preference
without rewriting deployment files. An external file edit still requires a
restart, because the running process cannot prove it is the intended operator
change.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import tomllib
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
import tomlkit
from dotenv import dotenv_values

from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.config.admission import (
    ApprovedCatalog,
    ProviderAllowlist,
    approved_groups_for,
    expected_catalog_allowlist,
    load_approved_catalog,
    parse_provider_allowlist,
)
from cairn.agent.models.config.loader import load_project_providers
from cairn.agent.models.config.schema import (
    CAIRN_LLM_API_KEY_ENV_NAME,
    DEFAULT_PROVIDER_NAME,
    ConfigLayout,
    parse_model_config,
    parse_provider_catalog,
    validate_runtime_provider,
)
from cairn.agent.models.registry import ModelSelectionError
from cairn.agent.models.runtime import ProviderActivation
from cairn.agent.models.selection import ModelConfig, ModelRef, ProviderConfig
from cairn.agent.models.selection_store import ModelSelectionStore
from cairn.server import host
from cairn.shared import platform

MAX_NAME_LENGTH = 120
MAX_BASE_URL_LENGTH = 2048
MAX_API_KEY_LENGTH = 4096
MAX_MODEL_ID_LENGTH = 500
MAX_MODEL_IDS = 100
MAX_PROBED_MODELS = 500
MAX_PROBE_BYTES = 2 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 10.0
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNQUOTED_DOTENV = re.compile(r"[A-Za-z0-9_./+=:@~-]+")

RESTART_COMMAND = "sudo ./scripts/start.sh restart"

#: Domestic, OpenAI-compatible endpoints first. Model IDs are suggestions only:
#: a provider may rename or retire one, so the page can probe ``/models``.
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "magpie",
        "name": "Magpie（本地）",
        "provider_name": "magpie",
        "base_url": "http://127.0.0.1:3425/v1",
        "model_ids": [],
        "note": "本地 OpenAI 兼容网关。启动 Magpie 后点「获取模型列表」；API Key 通常填 local。",
    },
    {
        "id": "bailian",
        "name": "阿里云百炼（千问）",
        "provider_name": "bailian",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_ids": ["qwen-plus"],
        "note": "阿里云通用兼容接口。自建 MaaS endpoint 请用「自定义」。",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "provider_name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model_ids": ["deepseek-chat"],
        "note": "DeepSeek 官方 OpenAI 兼容接口。",
    },
    {
        "id": "kimi",
        "name": "Kimi（月之暗面）",
        "provider_name": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model_ids": [],
        "note": "先填 API Key，再点「获取模型列表」选择可用模型。",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "provider_name": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model_ids": [],
        "note": "先填 API Key，再点「获取模型列表」选择可用模型。",
    },
    {
        "id": "ark",
        "name": "火山方舟（豆包）",
        "provider_name": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_ids": [],
        "note": "模型 ID 通常使用方舟推理接入点 ID（ep-…）；可先试「获取模型列表」。",
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "provider_name": "minimax",
        "base_url": "https://api.minimax.chat/v1",
        "model_ids": [],
        "note": "先填 API Key，再点「获取模型列表」选择可用模型。",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "provider_name": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model_ids": [],
        "note": "国产模型聚合接口；先填 API Key，再获取模型列表。",
    },
    {
        "id": "custom",
        "name": "自定义 OpenAI 兼容接口",
        "provider_name": "custom",
        "base_url": "",
        "model_ids": [],
        "note": "填写完整的 /v1 兼容 endpoint。远程地址必须使用 HTTPS。",
    },
)


class ModelSettingsError(ValueError):
    """A rejected configuration edit with a stable Web error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Runtime(Protocol):
    @property
    def provider(self) -> NamedProvider: ...

    @property
    def model_manager(self) -> Any: ...


class _Execution(Protocol):
    @property
    def provider_runtime(self) -> _Runtime | None: ...

    @property
    def selection_store(self) -> ModelSelectionStore: ...

    def current_model_ref(self) -> ModelRef | None: ...

    def model_catalog_signature(self) -> tuple[tuple[object, ...], ...]: ...

    def install_model_catalog(
        self,
        catalog: ApprovedCatalog,
        selection: ModelRef | None,
        *,
        persist: bool = True,
    ) -> ProviderActivation: ...

    def select_model(
        self, ref: ModelRef, *, persist: bool = True
    ) -> ProviderActivation: ...


@dataclass(frozen=True)
class SaveModelSettings:
    mode: str
    active_provider: str
    provider_name: str
    base_url: str
    api_key_env: str | None
    api_key: str | None
    active_group: str
    group_name: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProbeModels:
    base_url: str
    api_key_env: str | None
    api_key: str | None


@dataclass(frozen=True)
class _ProjectSnapshot:
    path: Path
    original: bytes | None
    text: str
    layout: ConfigLayout
    providers: tuple[NamedProvider, ...]
    mode: int = 0o600


@dataclass(frozen=True)
class _FileState:
    path: Path
    data: bytes | None
    mode: int


@dataclass
class _PendingWrite:
    state: _FileState
    after: bytes
    after_mode: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(code: str, message: str) -> ModelSettingsError:
    return ModelSettingsError(code, message)


def _clean_name(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > MAX_NAME_LENGTH
        or not cleaned.isprintable()
        or any(character.isspace() for character in cleaned)
    ):
        raise _error(
            "invalid_model_settings",
            f"{field_name}必须是非空、无空白字符的可打印文本。",
        )
    return cleaned


def _clean_base_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_BASE_URL_LENGTH:
        raise _error("invalid_model_settings", "Base URL 不能为空且不能过长。")
    return cleaned


def _clean_model_ids(values: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        model_id = value.strip()
        if (
            not model_id
            or len(model_id) > MAX_MODEL_ID_LENGTH
            or not model_id.isprintable()
            or any(character.isspace() for character in model_id)
        ):
            raise _error(
                "invalid_model_settings",
                "模型 ID 必须是非空、无空白字符的可打印文本。",
            )
        if model_id in cleaned:
            raise _error("invalid_model_settings", "模型 ID 不能重复。")
        cleaned.append(model_id)
    if not cleaned:
        raise _error("invalid_model_settings", "至少需要一个模型 ID。")
    if len(cleaned) > MAX_MODEL_IDS:
        raise _error("invalid_model_settings", "模型 ID 数量过多。")
    return tuple(cleaned)


def _clean_api_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not value:
        return None
    if (
        len(value) > MAX_API_KEY_LENGTH
        or not value.isprintable()
        or value != value.strip()
    ):
        raise _error("invalid_model_settings", "API Key 格式无效。")
    return value


def _clean_api_key_env(value: str | None, fallback: str) -> str:
    cleaned = (value or "").strip() or fallback
    if len(cleaned) > MAX_NAME_LENGTH or not _ENV_NAME.fullmatch(cleaned):
        raise _error(
            "invalid_model_settings", "凭据环境变量名必须由字母、数字和下划线组成。"
        )
    return cleaned


def _validated_provider_config(
    base_url: str, api_key_env: str, group_name: str, model_ids: tuple[str, ...]
) -> ProviderConfig:
    config = ProviderConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        model_config=(ModelConfig(name=group_name, model_ids=model_ids),),
    )
    try:
        validate_runtime_provider(config)
    except ValueError as error:
        raise _error("invalid_model_settings", str(error)) from None
    return config


def _read_approval(path: Path | None) -> tuple[ProviderAllowlist | None, bool]:
    if path is None:
        return None, False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, False
    if not stat.S_ISREG(info.st_mode):
        return None, False
    try:
        data = json.loads(path.read_bytes())
        return parse_provider_allowlist(data), True
    except (OSError, ValueError):
        return None, False


def _env_values(path: Path) -> dict[str, str | None]:
    try:
        return dict(dotenv_values(path))
    except (OSError, ValueError):
        return {}


def _key_state(
    api_key_env: str, env_values: Mapping[str, str | None]
) -> tuple[bool, str]:
    # The workspace .env is the Web-managed source. Report it even when the
    # running process also inherited the same name from its environment.
    if env_values.get(api_key_env):
        return True, "dotenv"
    if os.environ.get(api_key_env):
        return True, "host"
    return False, "missing"


def _default_key_env(name: str, used: set[str]) -> str:
    if CAIRN_LLM_API_KEY_ENV_NAME not in used:
        return CAIRN_LLM_API_KEY_ENV_NAME
    base = "CAIRN_LLM_API_KEY_" + re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    candidate = base[:MAX_NAME_LENGTH]
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = base[: MAX_NAME_LENGTH - len(tail)] + tail
        suffix += 1
    return candidate


def _new_provider_table(
    provider_name: str,
    base_url: str,
    api_key_env: str,
    group_name: str,
    model_ids: tuple[str, ...],
) -> Any:
    table = tomlkit.table()
    table["name"] = provider_name
    table["base_url"] = base_url
    table["api_key_env"] = api_key_env
    models = tomlkit.aot()
    models.append(_new_group_table(group_name, model_ids))
    table["models"] = models
    return table


def _new_group_table(group_name: str, model_ids: tuple[str, ...]) -> Any:
    group = tomlkit.table()
    group["name"] = group_name
    group["model_ids"] = list(model_ids)
    return group


def _new_catalog(
    provider_name: str,
    base_url: str,
    api_key_env: str,
    group_name: str,
    model_ids: tuple[str, ...],
) -> Any:
    document = tomlkit.document()
    providers = tomlkit.aot()
    providers.append(
        _new_provider_table(provider_name, base_url, api_key_env, group_name, model_ids)
    )
    document["providers"] = providers
    return document


def _legacy_to_catalog(document: Any) -> Any:
    catalog = tomlkit.document()
    for key, value in document.items():
        if key not in {"base_url", "api_key_env", "models"}:
            catalog[key] = value
    providers = tomlkit.aot()
    provider = tomlkit.table()
    provider["name"] = DEFAULT_PROVIDER_NAME
    provider["base_url"] = document["base_url"]
    provider["api_key_env"] = document["api_key_env"]
    provider["models"] = document["models"]
    providers.append(provider)
    catalog["providers"] = providers
    return catalog


def _provider_models(document: Any, catalog: bool, index: int = 0) -> Any:
    return document["providers"][index]["models"] if catalog else document["models"]


def _provider_table(document: Any, catalog: bool, index: int = 0) -> Any:
    return document["providers"][index] if catalog else document


def _find_index(items: Iterable[Any], name: str, field_name: str) -> int:
    for index, item in enumerate(items):
        if item.name == name:
            return index
    raise _error("invalid_model_settings", f"{field_name}不存在。")


def _replace_dotenv_value(text: str, key: str, value: str) -> str:
    line = f"{key}={_dotenv_value(value)}"
    pattern = re.compile(rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=.*$")
    lines = text.splitlines()
    replaced = False
    for index, existing in enumerate(lines):
        if pattern.fullmatch(existing):
            lines[index] = line
            replaced = True
    if not replaced:
        lines.append(line)
    return "\n".join(lines) + "\n"


def _dotenv_value(value: str) -> str:
    if _UNQUOTED_DOTENV.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_file(path: Path, content: bytes, mode: int, expected: bytes | None) -> None:
    """Atomically replace ``path`` only when its current bytes still match."""
    try:
        host.reject_symlinks(path)
        with host.parent_directory(path) as directory:
            temporary = f".{path.name}.{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | platform.flag("O_NOFOLLOW"),
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(content)
                    target.flush()
                    os.fchmod(target.fileno(), mode)
                    os.fsync(target.fileno())
                _require_unchanged(directory, path.name, expected)
                os.replace(
                    temporary,
                    path.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
    except platform.CapabilityMissing as error:
        raise _error(
            "model_settings_unavailable",
            "当前宿主机缺少安全写入所需的文件系统能力。",
        ) from error


def _require_unchanged(directory: int, name: str, expected: bytes | None) -> None:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | platform.flag("O_NOFOLLOW"), dir_fd=directory
        )
    except FileNotFoundError:
        if expected is not None:
            raise _error(
                "model_config_conflict",
                "配置文件在保存过程中被移除；请刷新后重试。",
            ) from None
        return
    with os.fdopen(descriptor, "rb") as current:
        if expected is None or current.read() != expected:
            raise _error(
                "model_config_conflict",
                "配置文件在保存过程中被其他操作修改；请刷新后重试。",
            )


def _remove_if_unchanged(path: Path, expected: bytes) -> None:
    host.reject_symlinks(path)
    with host.parent_directory(path) as directory:
        descriptor = os.open(
            path.name, os.O_RDONLY | platform.flag("O_NOFOLLOW"), dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as current:
            if current.read() != expected:
                raise _error(
                    "model_config_conflict",
                    "回滚时发现文件已被其他操作修改；请人工核对模型配置。",
                )
        os.unlink(path.name, dir_fd=directory)


def _read_state(path: Path) -> _FileState:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _FileState(path=path, data=None, mode=0o600)
    if not stat.S_ISREG(info.st_mode):
        raise _error(
            "model_settings_unavailable",
            f"配置文件不是普通文件：{path}",
        )
    try:
        return _FileState(
            path=path,
            data=path.read_bytes(),
            mode=stat.S_IMODE(info.st_mode),
        )
    except OSError as error:
        raise _error(
            "model_settings_unavailable", f"无法读取配置文件：{path}"
        ) from error


def _writable(path: Path) -> bool:
    try:
        host.reject_symlinks(path)
    except ValueError:
        return False
    target = path
    while not target.exists():
        if target.parent == target:
            return False
        target = target.parent
    return os.access(target, os.W_OK)


def _atomic_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


class ModelSettingsService:
    """Own the provider configuration the running Server serves.

    Reads describe the files and the active runtime without secrets. A save is
    one transaction: write the files, install the approved provider under the
    same rules as startup, and roll every file back if the new runtime cannot
    be built. Without a running provider (offline mode) the save stays pending
    until the next start.
    """

    def __init__(
        self,
        workspace: Path,
        state: Path,
        approval_path: Path | None,
        execution: _Execution,
    ) -> None:
        self.workspace = workspace.resolve()
        self.state = state.resolve()
        if approval_path is not None and not approval_path.is_absolute():
            raise ValueError("provider approval path must be absolute")
        self.approval_path = approval_path
        self.execution = execution
        self.config_path = self.workspace / ".cairn/models.toml"
        self.env_path = self.workspace / ".env"
        self.pending_path = self.state / "model-settings-pending.json"
        self._lock = threading.Lock()

    def clear_pending(self) -> None:
        """A successfully loaded provider process consumed the pending marker."""
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_locked()

    def save(self, request: SaveModelSettings) -> dict[str, Any]:
        with self._lock:
            return self._save_locked(request)

    def probe(self, request: ProbeModels) -> dict[str, Any]:
        env_values = _env_values(self.env_path)
        api_key_env = _clean_api_key_env(
            request.api_key_env, CAIRN_LLM_API_KEY_ENV_NAME
        )
        key = _clean_api_key(request.api_key)
        if key is None:
            key = env_values.get(api_key_env) or os.environ.get(api_key_env)
        if not key:
            raise _error(
                "credential_missing",
                f"未找到 {api_key_env}。请填写 API Key 后再获取模型列表。",
            )
        base_url = _clean_base_url(request.base_url)
        _validated_provider_config(base_url, api_key_env, "probe", ("probe",))
        return _probe_models(base_url, key)

    # -- read -------------------------------------------------------------

    def _load_project(self) -> _ProjectSnapshot:
        path = self.config_path
        try:
            info = path.lstat()
        except FileNotFoundError:
            return _ProjectSnapshot(
                path=path,
                original=None,
                text="",
                layout=ConfigLayout.ENV,
                providers=(),
            )
        if not stat.S_ISREG(info.st_mode):
            raise _error(
                "model_settings_unavailable",
                ".cairn/models.toml 不是普通文件。",
            )
        try:
            original = path.read_bytes()
            text = original.decode("utf-8")
            project = load_project_providers(path)
        except (OSError, UnicodeDecodeError, ValueError):
            raise _error(
                "model_config_invalid",
                "无法读取 .cairn/models.toml；请先在服务器上修复该文件。",
            ) from None
        return _ProjectSnapshot(
            path=path,
            original=original,
            text=text,
            layout=project.layout,
            providers=project.providers,
            mode=stat.S_IMODE(info.st_mode),
        )

    def _read_locked(self) -> dict[str, Any]:
        project = self._load_project()
        env_values = _env_values(self.env_path)
        allowlist, approval_valid = _read_approval(self.approval_path)
        providers = project.providers
        active_ref = self._effective_ref(project, allowlist)
        provider_views: list[dict[str, Any]] = []
        model_views: list[dict[str, str]] = []
        for provider in providers:
            key_configured, key_source = _key_state(
                provider.config.api_key_env, env_values
            )
            matched = (
                None if allowlist is None else approved_groups_for(allowlist, provider)
            )
            approved_ids = {group.name for group in matched or ()}
            groups = []
            for group in provider.config.model_config:
                active = (
                    active_ref is not None
                    and active_ref.provider == provider.name
                    and active_ref.group == group.name
                )
                groups.append(
                    {
                        "name": group.name,
                        "model_ids": list(group.model_ids),
                        "next_start": active,
                        "approved": group.name in approved_ids,
                        "active": active,
                    }
                )
                if group.name in approved_ids:
                    model_views.extend(
                        {
                            "provider": provider.name,
                            "group": group.name,
                            "model_id": model_id,
                        }
                        for model_id in group.model_ids
                    )
            provider_views.append(
                {
                    "name": provider.name,
                    "base_url": provider.config.base_url,
                    "api_key_env": provider.config.api_key_env,
                    "key_configured": key_configured,
                    "key_source": key_source,
                    "next_start": active_ref is not None
                    and active_ref.provider == provider.name,
                    "approved": matched is not None,
                    "groups": groups,
                }
            )

        runtime = self.execution.provider_runtime
        running = None
        if runtime is not None:
            running_group = runtime.model_manager.current_model()
            running = {
                "provider": runtime.provider.name,
                "group": running_group.name,
                "model": runtime.model_manager.current_model_id(),
                "base_url": runtime.provider.config.base_url,
            }

        restart_required = self._restart_required(project, allowlist, approval_valid)
        blocked_reason = self._blocked_reason()
        draft = None
        if not providers:
            draft = self._env_draft(env_values)
        return {
            "workspace": str(self.workspace),
            "config_path": str(self.config_path),
            "approval_path": (
                None if self.approval_path is None else str(self.approval_path)
            ),
            "config_exists": project.original is not None,
            "layout": project.layout.value,
            "providers": provider_views,
            "models": model_views,
            "active_provider": active_ref.provider if active_ref else None,
            "active_group": active_ref.group if active_ref else None,
            "active_model": active_ref.model if active_ref else None,
            "running": running,
            "offline": runtime is None,
            "approval_valid": approval_valid,
            "restart_required": restart_required,
            "restart_command": RESTART_COMMAND,
            "can_save": blocked_reason is None,
            "blocked_reason": blocked_reason,
            "draft": draft,
            "presets": list(PRESETS),
        }

    def _effective_ref(
        self, project: _ProjectSnapshot, allowlist: ProviderAllowlist | None
    ) -> ModelRef | None:
        ref = self.execution.current_model_ref()
        if ref is not None and self._ref_available(project, allowlist, ref):
            return ref
        for provider in project.providers:
            matched = (
                None if allowlist is None else approved_groups_for(allowlist, provider)
            )
            if matched:
                group = matched[0]
                return ModelRef(provider.name, group.name, group.model_ids[0])
        if project.providers:
            provider = project.providers[0]
            group = provider.config.model_config[0]
            return ModelRef(provider.name, group.name, group.model_ids[0])
        return None

    @staticmethod
    def _ref_available(
        project: _ProjectSnapshot,
        allowlist: ProviderAllowlist | None,
        ref: ModelRef,
    ) -> bool:
        provider = next(
            (item for item in project.providers if item.name == ref.provider),
            None,
        )
        if provider is None or allowlist is None:
            return False
        matched = approved_groups_for(allowlist, provider)
        if not matched:
            return False
        return any(
            group.name == ref.group and ref.model in group.model_ids
            for group in matched
        )

    def _env_draft(self, env_values: Mapping[str, str | None]) -> dict[str, Any]:
        def setting(name: str) -> str:
            return os.environ.get(name) or env_values.get(name) or ""

        model = setting("CAIRN_LLM_MODEL")
        base_url = setting("CAIRN_BASE_URL")
        key_configured, key_source = _key_state(CAIRN_LLM_API_KEY_ENV_NAME, env_values)
        return {
            "provider_name": DEFAULT_PROVIDER_NAME,
            "base_url": base_url,
            "api_key_env": CAIRN_LLM_API_KEY_ENV_NAME,
            "group_name": model or "default",
            "model_ids": [model] if model else [],
            "key_configured": key_configured,
            "key_source": key_source,
        }

    def _restart_required(
        self,
        project: _ProjectSnapshot,
        allowlist: ProviderAllowlist | None,
        approval_valid: bool,
    ) -> bool:
        if self.pending_path.exists():
            return True
        providers = project.providers
        if not providers:
            return not approval_valid and self.approval_path is not None
        if not approval_valid or allowlist is None:
            return True
        runtime = self.execution.provider_runtime
        if runtime is None:
            return False
        expected = tuple(
            (
                provider.name,
                provider.config.base_url,
                provider.config.api_key_env,
                tuple(
                    (group.name, tuple(group.model_ids))
                    for group in (approved_groups_for(allowlist, provider) or ())
                ),
            )
            for provider in providers
            if approved_groups_for(allowlist, provider) is not None
        )
        return expected != self.execution.model_catalog_signature()

    def _blocked_reason(self) -> str | None:
        if self.approval_path is None:
            return "approval_path_missing"
        if not _writable(self.config_path):
            return "config_not_writable"
        if not _writable(self.env_path):
            return "env_not_writable"
        if not _writable(self.approval_path):
            return "approval_not_writable"
        if not _writable(self.execution.selection_store.path):
            return "state_not_writable"
        if not _writable(self.pending_path):
            return "state_not_writable"
        return None

    # -- save -------------------------------------------------------------

    def _save_locked(self, request: SaveModelSettings) -> dict[str, Any]:
        project = self._load_project()
        env_state = _read_state(self.env_path)
        if self.approval_path is None:
            raise _error(
                "model_settings_unavailable",
                "Server 没有配置 provider_approval，无法保存模型设置。",
            )
        approval_state = _read_state(self.approval_path)
        approval_path = self.approval_path
        pending_state = _read_state(self.pending_path)
        selection_state = _read_state(self.execution.selection_store.path)
        env_values = _env_values(self.env_path)

        mode = request.mode.strip().lower()
        if mode not in {"create", "update"}:
            raise _error("invalid_model_settings", "保存模式无效。")
        if project.original is None:
            mode = "create"
        if mode == "update" and not any(
            provider.name == request.active_provider for provider in project.providers
        ):
            raise _error("invalid_model_settings", "要更新的提供方不存在。")

        provider_name = _clean_name(request.provider_name, "提供方名称")
        group_name = _clean_name(request.group_name, "模型组名称")
        model_ids = _clean_model_ids(request.model_ids)
        base_url = _clean_base_url(request.base_url)

        existing_names = {provider.name for provider in project.providers}
        if (mode == "create" and provider_name in existing_names) or (
            mode == "update"
            and provider_name in existing_names
            and provider_name != request.active_provider
        ):
            raise _error("invalid_model_settings", "提供方名称已存在。")

        existing_key_env = ""
        if mode == "update":
            existing = next(
                provider
                for provider in project.providers
                if provider.name == request.active_provider
            )
            existing_key_env = existing.config.api_key_env
            _find_index(existing.config.model_config, request.active_group, "模型组")
        used_key_envs = {
            provider.config.api_key_env
            for provider in project.providers
            if mode == "create" or provider.name != request.active_provider
        }
        fallback_key_env = (
            existing_key_env
            or _default_key_env(provider_name, used_key_envs)
            or CAIRN_LLM_API_KEY_ENV_NAME
        )
        api_key_env = _clean_api_key_env(request.api_key_env, fallback_key_env)
        if mode == "create" and api_key_env in used_key_envs:
            raise _error(
                "invalid_model_settings",
                "该凭据环境变量已被其他提供方使用，请换一个名称。",
            )
        _validated_provider_config(base_url, api_key_env, group_name, model_ids)

        provided_key = _clean_api_key(request.api_key)
        # A workspace .env entry is the managed source and is loaded by the
        # service EnvironmentFile on restart, so replacing it is safe even when
        # the running process also sees the old value in its environment.
        if (
            provided_key is not None
            and os.environ.get(api_key_env)
            and api_key_env not in env_values
        ):
            raise _error(
                "host_env_credential",
                f"{api_key_env} 由服务环境提供且不在工作区 .env 中，Web 不能覆盖。"
                "请使用其他凭据变量名，或更新服务的环境变量后重启。",
            )
        key_configured, _key_source = _key_state(api_key_env, env_values)
        if provided_key is None and not key_configured:
            raise _error(
                "credential_missing",
                f"{api_key_env} 尚未配置。请填写 API Key 后再保存。",
            )

        document = self._edited_document(
            project,
            mode,
            request,
            provider_name,
            base_url,
            api_key_env,
            group_name,
            model_ids,
        )
        updated = tomlkit.dumps(document)
        providers = self._validated_providers(updated)
        provider = next(
            (item for item in providers if item.name == provider_name),
            None,
        )
        if provider is None:
            raise _error(
                "model_settings_unavailable",
                "保存后的提供方不存在；请刷新后重试。",
            )
        group = next(
            (item for item in provider.config.model_config if item.name == group_name),
            None,
        )
        if group is None or not group.model_ids:
            raise _error(
                "model_settings_unavailable",
                "保存后的模型组不存在；请刷新后重试。",
            )
        preferred = ModelRef(provider_name, group_name, group.model_ids[0])
        approval_bytes = _atomic_json(asdict(expected_catalog_allowlist(providers)))
        selection_bytes = self.execution.selection_store.encode(preferred)

        env_text = (
            _replace_dotenv_value(
                self.env_state_text(env_state), api_key_env, provided_key
            )
            if provided_key is not None
            else None
        )
        pending_bytes = _atomic_json({"version": 1, "saved_at": _now()})

        writes: list[_PendingWrite] = []
        self._plan_write(
            writes,
            project.path,
            project.original,
            updated.encode("utf-8"),
            project.mode,
            project.mode,
        )
        if env_text is not None:
            self._plan_write(
                writes,
                self.env_path,
                env_state.data,
                env_text.encode("utf-8"),
                env_state.mode,
                0o600,
            )
        self._plan_write(
            writes,
            self.approval_path,
            approval_state.data,
            approval_bytes,
            approval_state.mode,
            0o600,
        )
        self._plan_write(
            writes,
            self.execution.selection_store.path,
            selection_state.data,
            selection_bytes,
            selection_state.mode,
            0o600,
        )
        self._plan_write(
            writes,
            self.pending_path,
            pending_state.data,
            pending_bytes,
            pending_state.mode,
            0o600,
        )

        for write in writes:
            try:
                host.reject_symlinks(write.state.path)
            except ValueError as error:
                raise _error("model_settings_unavailable", str(error)) from error
            if not _writable(write.state.path):
                raise _error(
                    "model_settings_unavailable",
                    f"没有写入权限：{write.state.path}",
                )

        completed: list[_PendingWrite] = []
        applied = False
        try:
            for write in writes:
                _write_file(
                    write.state.path,
                    write.after,
                    write.after_mode,
                    write.state.data,
                )
                completed.append(write)
            # Prove the written files are readable before rebinding anything.
            self._read_locked()
            applied = self._install_saved_catalog(approval_path, preferred)
        except BaseException as error:
            failures = self._rollback(completed)
            if failures:
                raise _error(
                    "model_settings_rollback_failed",
                    "保存失败且回滚不完整；请立即在服务器上核对 models.toml、.env 和 provider approval。",
                ) from error
            raise
        # The install never raises after the swap; a read failure here leaves
        # files and runtime consistent and must not roll either one back.
        current = self._read_locked()

        return {
            "settings": current,
            "restart_required": current["restart_required"],
            "restart_command": RESTART_COMMAND,
            "message": (
                "模型配置已保存并立即生效；新的对话回合会使用新的模型。"
                if applied
                else "模型配置已保存；当前没有运行中的提供方，重启 Cairn Server 后生效。"
            ),
        }

    def _install_saved_catalog(self, approval_path: Path, preferred: ModelRef) -> bool:
        """Install the catalog the save just wrote, or stay pending.

        The workspace ``.env`` is the Web-managed source, so its values win for
        this reload. A process started by systemd may still carry the previous
        credential in ``os.environ``; preferring that copy would silently keep
        using a key the operator just replaced.  Selection is already part of
        the file transaction, so this method must not persist it again.
        """
        if self.execution.provider_runtime is None:
            return False
        managed = {
            name: value
            for name, value in dotenv_values(self.env_path).items()
            if value is not None
        }
        try:
            allowlist = parse_provider_allowlist(json.loads(approval_path.read_bytes()))
            catalog = load_approved_catalog(
                self.workspace,
                allowlist,
                host_env={**os.environ, **managed},
            )
            self.execution.install_model_catalog(catalog, preferred, persist=False)
        except (OSError, ValueError) as error:
            # The files are already written; the caller rolls them back so the
            # next start cannot disagree with the runtime that stayed active.
            raise _error(
                "model_settings_unavailable",
                f"新的模型配置无法启用（{type(error).__name__}）；本次保存已回滚。",
            ) from error
        self.clear_pending()
        return True

    def select(self, ref: ModelRef) -> dict[str, Any]:
        """Switch the running default without touching deployment files."""
        with self._lock:
            if self.execution.provider_runtime is None:
                raise _error(
                    "model_selection_unavailable",
                    "当前没有运行中的提供方，无法切换模型。",
                )
            try:
                self.execution.select_model(ref)
            except ModelSelectionError:
                raise _error(
                    "invalid_model_selection",
                    "该模型不在宿主已批准的提供方和模型组内。",
                ) from None
            return {
                "settings": self._read_locked(),
                "message": f"已切换到 {ref.provider} / {ref.group} / {ref.model}；新的回合立即使用。",
            }

    @staticmethod
    def env_state_text(state: _FileState) -> str:
        if state.data is None:
            return ""
        try:
            return state.data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise _error(
                "model_settings_unavailable",
                "工作区 .env 不是 UTF-8 文本；请先修复该文件。",
            ) from None

    @staticmethod
    def _plan_write(
        writes: list[_PendingWrite],
        path: Path,
        before: bytes | None,
        after: bytes,
        before_mode: int,
        after_mode: int,
    ) -> None:
        if before == after:
            return
        writes.append(
            _PendingWrite(
                state=_FileState(path=path, data=before, mode=before_mode),
                after=after,
                after_mode=after_mode,
            )
        )

    def _rollback(self, completed: list[_PendingWrite]) -> list[Path]:
        failures: list[Path] = []
        for write in reversed(completed):
            try:
                if write.state.data is None:
                    _remove_if_unchanged(write.state.path, write.after)
                else:
                    _write_file(
                        write.state.path,
                        write.state.data,
                        write.state.mode,
                        write.after,
                    )
            except (OSError, ValueError):
                failures.append(write.state.path)
        return failures

    def _edited_document(
        self,
        project: _ProjectSnapshot,
        mode: str,
        request: SaveModelSettings,
        provider_name: str,
        base_url: str,
        api_key_env: str,
        group_name: str,
        model_ids: tuple[str, ...],
    ) -> Any:
        if project.original is None:
            document = _new_catalog(
                provider_name, base_url, api_key_env, group_name, model_ids
            )
            return document

        catalog = project.layout is ConfigLayout.CATALOG
        document = tomlkit.parse(project.text)
        if not catalog and (mode == "create" or provider_name != DEFAULT_PROVIDER_NAME):
            document = _legacy_to_catalog(document)
            catalog = True
        if mode == "create":
            providers = document["providers"] if catalog else None
            if providers is None:
                raise _error(
                    "model_settings_unavailable", "无法把当前配置升级为多提供方格式。"
                )
            providers.append(
                _new_provider_table(
                    provider_name, base_url, api_key_env, group_name, model_ids
                )
            )
        else:
            if catalog:
                provider_index = next(
                    (
                        index
                        for index, provider in enumerate(document["providers"])
                        if provider["name"] == request.active_provider
                    ),
                    None,
                )
                if provider_index is None:
                    raise _error("invalid_model_settings", "要更新的提供方不存在。")
            else:
                provider_index = 0
            provider = _provider_table(document, catalog, provider_index)
            if catalog:
                provider["name"] = provider_name
            provider["base_url"] = base_url
            provider["api_key_env"] = api_key_env
            models = _provider_models(document, catalog, provider_index)
            group_index = next(
                (
                    index
                    for index, group in enumerate(models)
                    if group["name"] == request.active_group
                ),
                None,
            )
            if group_index is None:
                raise _error("invalid_model_settings", "要更新的模型组不存在。")
            group = models[group_index]
            group["name"] = group_name
            group["model_ids"] = list(model_ids)
        return document

    @staticmethod
    def _validated_providers(text: str) -> tuple[NamedProvider, ...]:
        try:
            values = tomllib.loads(text)
            if "providers" in values:
                catalog = parse_provider_catalog(values)
                providers = catalog.providers
            else:
                config = parse_model_config(values)
                validate_runtime_provider(config)
                providers = (NamedProvider(name=DEFAULT_PROVIDER_NAME, config=config),)
        except ValueError:
            raise _error(
                "invalid_model_settings",
                "保存后的配置未通过校验；未写入任何文件。",
            ) from None
        if not providers or any(
            not provider.config.model_config for provider in providers
        ):
            raise _error("invalid_model_settings", "每个提供方必须包含至少一个模型组。")
        return tuple(providers)


def _probe_models(base_url: str, api_key: str) -> dict[str, Any]:
    endpoint = base_url.rstrip("/") + "/models"
    try:
        with (
            httpx.Client(
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=5.0),
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "GET",
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            ) as response,
        ):
            if response.status_code in {401, 403}:
                raise _error(
                    "probe_credential_rejected",
                    "提供方拒绝了该 API Key。请检查 Key 或 endpoint。",
                )
            if response.status_code == 404:
                raise _error(
                    "probe_failed",
                    "endpoint 没有 /models 接口；可以直接手填模型 ID。",
                )
            if not 200 <= response.status_code < 300:
                raise _error(
                    "probe_failed",
                    f"提供方返回 HTTP {response.status_code}；可以直接手填模型 ID。",
                )
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PROBE_BYTES:
                    raise _error("probe_failed", "提供方返回的模型列表过大。")
    except httpx.TimeoutException as error:
        raise _error(
            "probe_timeout", "连接提供方超时；请检查 endpoint 或网络。"
        ) from error
    except httpx.HTTPError as error:
        raise _error(
            "probe_failed", "无法连接提供方；请检查 endpoint 或网络。"
        ) from error

    try:
        payload = json.loads(body)
    except ValueError as error:
        raise _error(
            "probe_failed", "提供方没有返回 JSON 模型列表；可以直接手填模型 ID。"
        ) from error
    raw_models = None
    if isinstance(payload, dict):
        raw_models = payload.get("data", payload.get("models"))
    elif isinstance(payload, list):
        raw_models = payload
    models: list[str] = []
    if isinstance(raw_models, list):
        for item in raw_models:
            model_id = item.get("id") if isinstance(item, dict) else item
            if (
                isinstance(model_id, str)
                and model_id
                and model_id not in models
                and len(models) < MAX_PROBED_MODELS
            ):
                models.append(model_id)
    return {"endpoint": endpoint, "models": models}
