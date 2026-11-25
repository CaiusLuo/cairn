#: Provider error codes that mark the credential/account rather than one model.
#: Another candidate behind the same API key cannot clear them. Model Studio
#: reuses ``insufficient_quota`` for a per-model free-tier stop, so only the
#: remaining codes are unconditionally account-level.
_SHARED_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})
_ACCOUNT_BILLING_CODES = _SHARED_QUOTA_CODES - {"insufficient_quota"}

#: Code for one model that is unavailable, which another candidate can clear.
_MODEL_NOT_FOUND_CODES = frozenset({"model_not_found"})

#: Code for one model's free-tier allowance (Alibaba Cloud Model Studio). It is
#: model-level: another configured model keeps its own free-tier allowance.
_FREE_TIER_QUOTA_CODES = frozenset({"AllocationQuota.FreeTierOnly"})

#: Fragments of the documented Model Studio 403 message, matched only when
#: LiteLLM rebuilt the error without the provider payload. Both must be present,
#: which no generic 403, credential failure or unrelated APIError satisfies.
_FREE_TIER_403_SIGNATURE = ("free quota exhausted", "use free tier only")

#: ``/model list`` diagnostics stay on one bounded line.
_DIAGNOSTIC_LIMIT = 80


def _short_diagnostic(error: Exception) -> str:
    """Collapse a provider diagnostic to one bounded, single-line string.

    LiteLLM errors prefix their own class name in ``str()``; the category is
    already printed separately. ``LiteLLMClient`` replaces credential-bearing
    provider errors before they reach this layer, so this only normalizes
    whitespace and length.
    """
    text = " ".join(str(error).split())
    prefix = f"litellm.{type(error).__name__}: "
    if text.startswith(prefix):
        text = text[len(prefix) :]
    if len(text) <= _DIAGNOSTIC_LIMIT:
        return text
    return text[: _DIAGNOSTIC_LIMIT - 1].rstrip() + "…"


def _structured_error_fields(error: BaseException) -> set[str]:
    """Collect structured ``code``/``type`` values along the exception chain.

    LiteLLM 1.101 rebuilds provider errors with ``body=None`` and synthetic
    ``code``/``type`` values; the provider's parsed payload survives only on the
    chained original exception. Only structured fields are read; message text is
    never parsed here.
    """
    fields: set[str] = set()
    current: BaseException | None = error
    while current is not None:
        sources: list[object] = [current]
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            sources.extend((body, body.get("error")))
        for source in sources:
            if isinstance(source, dict):
                values = (source.get("code"), source.get("type"))
            else:
                values = (getattr(source, "code", None), getattr(source, "type", None))
            fields.update(value for value in values if isinstance(value, str))
        current = current.__context__
    return fields


def _records_shared_quota(error: BaseException) -> bool:
    """Whether structured provider fields mark the account out of quota."""
    return bool(_structured_error_fields(error) & _SHARED_QUOTA_CODES)


def _is_model_free_tier_exhaustion(error: BaseException) -> bool:
    """Whether one model's free-tier allowance is exhausted.

    Prefer the structured provider code, which survives on the chained original
    exception. LiteLLM otherwise rebuilds a bare ``APIError`` with ``body=None``
    (its openai mapper has no 403 branch), so the documented 403 message is the
    remaining narrow signature.

    Model Studio also labels this stop ``insufficient_quota``, its generic quota
    taxonomy, so that code cannot mean the account here: the documented message
    names the free tier, whose allowance belongs to one model. A hard billing
    limit stays account-level and never falls back.
    """
    fields = _structured_error_fields(error)
    if fields & _ACCOUNT_BILLING_CODES:
        return False
    if fields & _FREE_TIER_QUOTA_CODES:
        return True
    if getattr(error, "status_code", None) != 403:
        return False
    text = str(error).lower()
    return all(fragment in text for fragment in _FREE_TIER_403_SIGNATURE)


def is_fallback_error(error: BaseException) -> bool:
    """Whether trying the next candidate model could plausibly succeed.

    V1 falls back on a vendor rate limit: LiteLLM reports that the upstream
    provider throttled this model (not LiteLLM's own key/team/model limiter) and
    no structured payload marks the account itself as out of quota. It also
    falls back when one model's free-tier allowance is exhausted (Alibaba Cloud
    Model Studio ``AllocationQuota.FreeTierOnly`` or its documented 403 message,
    which another candidate's own allowance can clear), and when the selected
    model itself is unavailable (a 404 or a structured ``model_not_found``),
    which another candidate can replace.

    Everything else propagates: cancellation, malformed requests, authentication
    failures, context overflow, timeouts, service-unavailable responses and
    ambiguous billing failures. Timeouts and 503s are not model-level, and every
    candidate shares this endpoint and credential, so retrying cannot be shown to
    help.

    LiteLLM's 1.101 SDK path discards the provider payload, so a vendor 429 or
    the free-tier 403 cannot always be classified from structured fields and is
    then matched against its documented message. That fallback stays bounded to
    the configured candidates and never persists, so the residual ambiguity is
    cheap.
    """
    # Keep the provider SDK out of the core/CLI import path until needed.
    from litellm.exceptions import (
        NotFoundError,
        RateLimitError,
        RateLimitErrorCategory,
        RateLimitType,
    )

    if isinstance(error, NotFoundError):
        return True
    if _structured_error_fields(error) & _MODEL_NOT_FOUND_CODES:
        return True
    if not isinstance(error, RateLimitError):
        return _is_model_free_tier_exhaustion(error)
    if error.category != RateLimitErrorCategory.VENDOR_RATE_LIMIT.value:
        # LiteLLM's own key/team/model limiter or a proxy budget stop: shared.
        return False
    if error.rate_limit_type in (
        RateLimitType.BUDGET.value,
        RateLimitType.MAX_ITERATIONS.value,
    ):
        return False
    return not _records_shared_quota(error)


def fallback_reason(error: BaseException) -> str:
    """Fixed diagnostic for a failure the executor already classified as retryable."""
    if _is_model_free_tier_exhaustion(error):
        return "model_quota"
    return "provider_rate_limit"
