"""Environment policy for local Git worktree lifecycle subprocesses."""

from collections.abc import Mapping

# HOME/XDG_CONFIG_HOME keep normal global config discovery; PATH selects Git.
# Do not inherit shell, Python, dynamic-loader or Git configuration overrides.
GIT_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}
)
GIT_SECRET_ENV_KEYS = frozenset({"CAIRN_LLM_API_KEY"})


def build_git_env(
    host_env: Mapping[str, str], secret_env_keys: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Copy only local-operation settings, excluding every declared credential."""
    excluded = GIT_SECRET_ENV_KEYS | secret_env_keys
    env = {
        name: host_env[name]
        for name in GIT_ENV_ALLOWLIST - excluded
        if name in host_env
    }
    if "GIT_TERMINAL_PROMPT" not in excluded:
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env
