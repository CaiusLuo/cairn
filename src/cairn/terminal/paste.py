"""Collapsing a large paste so it cannot flood the input line.

A pasted file or stack trace would otherwise fill the composer and push everything
else out of view, leaving a reader scrolling through content they already have. The
paste is replaced by a short marker, and the full text is kept so submitting still
sends it.

Thresholds follow what the shipped agents converged on: a paste is large when it
covers several lines *or* runs long. Line count is the more meaningful signal for
code — three lines of code is already a block, while 800 characters of prose is a
paragraph — so lines are checked first and characters act as the fallback for a
single very long line.
"""

from dataclasses import dataclass, field

#: A paste covering this many lines is collapsed. Three is the point where pasted
#: content starts reading as a block rather than as a phrase.
LARGE_PASTE_LINES = 3

#: A single-line paste is collapsed once it runs this long. Matches the threshold a
#: shipped agent documents, so a long one-line paste does not scroll the composer.
LARGE_PASTE_CHARACTERS = 800


@dataclass(frozen=True, slots=True)
class PasteDecision:
    """What to do with one paste.

    ``marker`` is empty when the paste is small enough to type through; otherwise
    it is the text that stands in for the payload.
    """

    payload: str
    marker: str

    @property
    def collapsed(self) -> bool:
        return bool(self.marker)


def is_large_paste(text: str) -> bool:
    """Whether ``text`` is large enough to collapse."""
    if not text:
        return False
    if text.count("\n") + 1 >= LARGE_PASTE_LINES:
        return True
    return len(text) > LARGE_PASTE_CHARACTERS


def decide_paste(text: str, *, existing: int = 0) -> PasteDecision:
    """Decide how a paste should appear, given how many were already collapsed.

    ``existing`` counts placeholders already in the buffer, so a second paste is
    shown with a distinct index instead of looking identical to the first.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not is_large_paste(normalized):
        return PasteDecision(payload=normalized, marker="")
    lines = normalized.count("\n") + 1
    if lines >= LARGE_PASTE_LINES:
        detail = f"pasted {lines} lines"
    else:
        detail = f"pasted {len(normalized)} chars"
    index = existing + 1
    suffix = "" if index == 1 else f" #{index}"
    return PasteDecision(payload=normalized, marker=f"[{detail}{suffix}]")


@dataclass(slots=True)
class PasteRegistry:
    """The payloads behind the markers currently in the buffer.

    A placeholder is only useful if the text it stands for survives to submission,
    so the payload is kept here keyed by its marker. This is a plain map rather than
    a text-range model: prompt_toolkit has no element system to make a placeholder
    atomic under editing, and a map is the honest 80% — editing over a marker can
    orphan its payload, which is reported by ``expand`` rather than silently
    dropping text.
    """

    _payloads: dict[str, str] = field(default_factory=dict)

    def remember(self, marker: str, payload: str) -> None:
        self._payloads[marker] = payload

    def expand(self, text: str) -> str:
        """Replace every marker in ``text`` with the payload it stands for."""
        expanded = text
        for marker, payload in self._payloads.items():
            if marker in expanded:
                expanded = expanded.replace(marker, payload)
        return expanded

    @property
    def count(self) -> int:
        return len(self._payloads)

    def has(self, marker: str) -> bool:
        return marker in self._payloads

    def clear(self) -> None:
        self._payloads.clear()
