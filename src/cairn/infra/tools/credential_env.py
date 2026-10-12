"""The one list of Cairn-owned credential names stripped from child processes.

`bash.build_command_env` (host fidelity, credentials removed) and
`git.environment.build_git_env` (allowlist) are deliberately opposite policies,
but both must remove exactly these names at the same trust boundary.
"""

#: Cairn-owned credentials that must never reach a child command or Git process.
SECRET_ENV_KEYS = frozenset({"CAIRN_LLM_API_KEY"})
