"""Environment policy for local Git worktree lifecycle subprocesses."""

import os
from collections.abc import Mapping

# HOME supports explicitly included repository config paths; PATH selects Git.
# Do not inherit shell, Python, dynamic-loader or Git configuration overrides.
GIT_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}
)
GIT_SECRET_ENV_KEYS = frozenset({"CAIRN_LLM_API_KEY"})
GIT_CONFIG_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def build_git_env(
    host_env: Mapping[str, str], secret_env_keys: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Copy only local-operation settings, excluding every declared credential."""
    excluded = GIT_SECRET_ENV_KEYS | secret_env_keys
    if excluded.intersection(GIT_CONFIG_ENV):
        raise ValueError("Credential names conflict with Git configuration isolation")
    env = {
        name: host_env[name]
        for name in GIT_ENV_ALLOWLIST - excluded
        if name in host_env
    }
    # These are owned policy values, never inherited configuration overrides.
    # Local/worktree config and their includes remain effective for every call.
    env.update(GIT_CONFIG_ENV)
    if "GIT_TERMINAL_PROMPT" not in excluded:
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env
