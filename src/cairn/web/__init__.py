"""Read-only web surface adapter for Cairn.

The web package is an outer presentation adapter, parallel to ``cairn.terminal``.
It reads committed Session facts and shared TaskBoard facts; it does not create
an Agent loop or mutate either store.
"""

from cairn.web.app import create_app

__all__ = ["create_app"]
