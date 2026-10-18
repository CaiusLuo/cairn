"""Provider clients and error translation.

Importing this package settles how LiteLLM obtains its model cost map, before any
submodule imports the SDK.

LiteLLM fetches that map over the network at *import* time, so a bare
``import litellm`` is an external call. Leaving it enabled made every entry point
that merely loads a Cairn module depend on network latency: ``--help`` on the
evaluation runners, and the tests that check they still start. Debian reported
the failure as a 30-second timeout against a slow endpoint, which reads like a
broken script rather than a stalled download.

The package ships the map, so no capability is lost — only the refresh. Offline is
the default because an import must not reach the network; a caller that wants a
fresher map can set ``LITELLM_LOCAL_MODEL_COST_MAP=False`` before importing
Cairn, and the environment keeps the last word.
"""

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
