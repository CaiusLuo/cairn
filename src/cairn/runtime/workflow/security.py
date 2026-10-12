"""Credentials that must never reach the coding task or its tools."""

GITHUB_SECRET_ENV_KEYS = frozenset({"CAIRN_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"})
