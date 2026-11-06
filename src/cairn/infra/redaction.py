"""Credential redaction for host telemetry and transient presentation."""

from typing import Any


def redact_text(text: str, secret_values: "tuple[str, ...]") -> str:
    clean = text
    for value in sorted(secret_values, key=len, reverse=True):
        if value:
            clean = clean.replace(value, "[redacted]")
    return clean


def redact_value(value: object, secret_values: "tuple[str, ...]") -> Any:
    if isinstance(value, str):
        return redact_text(value, secret_values)
    if isinstance(value, list):
        return [redact_value(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secret_values) for item in value)
    if isinstance(value, dict):
        return {
            redact_text(key, secret_values)
            if isinstance(key, str)
            else key: redact_value(item, secret_values)
            for key, item in value.items()
        }
    return value


class StreamRedactor:
    """Hold a bounded suffix so a declared secret split across deltas stays private."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self.pending = ""
        self.reserve = max((len(s) for s in self.secrets), default=1) - 1

    def feed(self, text: str, *, final: bool = False) -> str:
        self.pending += text
        cut = len(self.pending) if final else max(0, len(self.pending) - self.reserve)
        # Never cut a complete secret in half before replacing it.
        while True:
            previous = cut
            for secret in self.secrets:
                offset = 0
                while (pos := self.pending.find(secret, offset)) >= 0:
                    if pos < cut < pos + len(secret):
                        cut = pos
                    offset = pos + 1
            if previous == cut:
                break
        value, self.pending = self.pending[:cut], self.pending[cut:]
        return redact_text(value, self.secrets)
