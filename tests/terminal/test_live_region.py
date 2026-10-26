"""The seam between the streaming display and whatever paints it.

Two hosts exist because they cannot coexist: ``rich.Live`` moves the cursor itself,
and an application that owns the bottom of the terminal repaints over it. These
tests pin the part both must honour — start, replace, stop — so the display can be
switched between them without knowing which it has.
"""

from rich.text import Text

from cairn.terminal.live import LiveRegion
from cairn.terminal.output import (
    LiveFactory,
    StreamingDisplay,
    _RichLiveRegion,
    rich_live_region,
)


class _Recorder:
    """A live region that records what it was asked to do."""

    def __init__(self) -> None:
        self.started: list[object] = []
        self.updated: list[object] = []
        self.stopped = 0

    def start(self, renderable: object) -> None:
        self.started.append(renderable)

    def update(self, renderable: object) -> None:
        self.updated.append(renderable)

    def flush(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped += 1


def test_the_rich_region_satisfies_the_protocol() -> None:
    assert isinstance(_RichLiveRegion(lambda: Text("")), LiveRegion)


def test_the_factory_returns_a_region() -> None:
    assert isinstance(rich_live_region(lambda: Text("")), LiveRegion)


def test_the_display_defaults_to_the_rich_host() -> None:
    """A plain terminal keeps the cursor-moving behaviour it expects."""
    display = StreamingDisplay()
    assert display._live_factory is rich_live_region


def test_an_injected_host_replaces_the_default() -> None:
    recorder = _Recorder()

    def factory(frame: object) -> LiveRegion:
        return recorder

    display = StreamingDisplay(live_factory=factory)
    assert display._live_factory is factory


def test_starting_the_region_asks_the_host_to_draw() -> None:
    recorder = _Recorder()

    def factory(frame: object) -> LiveRegion:
        return recorder

    display = StreamingDisplay(live_factory=factory)
    display._start_live()

    assert len(recorder.started) == 1


def test_closing_the_region_stops_the_host() -> None:
    recorder = _Recorder()

    def factory(frame: object) -> LiveRegion:
        return recorder

    display = StreamingDisplay(live_factory=factory)
    display._start_live()
    display._close_live()

    assert recorder.stopped == 1


def test_closing_without_starting_is_harmless() -> None:
    """A cancelled attempt can close a region it never opened."""
    display = StreamingDisplay()
    display._close_live()
    assert display._live is None


def test_the_factory_type_is_importable() -> None:
    """The alias documents the seam, so it must stay exported."""
    assert LiveFactory is not None
