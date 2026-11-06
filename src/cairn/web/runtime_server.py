"""Compatibility imports; Cairn Server assembly lives in cairn.server."""

from cairn.server import app as app
from cairn.server import main as main
from cairn.server import runtime_app as runtime_app
from cairn.server import serve as serve

if __name__ == "__main__":
    main()
