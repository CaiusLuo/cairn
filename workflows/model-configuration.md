# Model configuration and requests

## Trigger

A Cairn session starts, the user changes model/provider or credentials, or the
user sends a request after editing `.cairn/models.toml`.

## Behavior

- Start with the first provider, its first model group, and its first model ID in
  file order. Do not ask for a model choice or provider authorization.
- Keep the selected model visible near the conversation input. A user may
  manually select another configured provider, group, or model ID without an
  additional provider authorization prompt.
- A missing API key must not prevent entering the conversation. A request with
  a missing key fails with the relevant error and does not try another model.
- A Cairn command accepts a key through hidden input, saves it in `.env`, and
  stores only its environment-variable reference in `.cairn/models.toml`.
  It becomes effective in the same session. The Agent must not be able to obtain
  the plaintext via file tools, Bash, or local MCP; never put it in conversation
  text, history, trace, logs, or process args.
- Changes saved to the model configuration, including provider endpoint,
  credential, and model list, apply before subsequent requests without restarting.
  Validate and build the replacement before committing it; invalid configuration
  fails the request and never silently switches models or uses stale routing.
- Make one model request at a time. On failure, display the real provider error
  with credentials redacted; do not automatically retry or fall back. The user
  decides whether to amend configuration or select another model.
- Preserve conversation history and existing workspace/tool permissions.

## Acceptance

Use offline tests with fake keys. Cover startup order, missing-key startup and
failure, hidden key input and persistence, restart and reload, manual ID and
provider switching, invalid reload, credential filtering, streamed and
non-streamed failures, and absence of automatic retries/fallback.
