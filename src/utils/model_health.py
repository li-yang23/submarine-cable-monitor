"""Model-account preflight without assuming that a retired balance API works."""

from typing import Any


def check_model_health(
    api_key: str | None,
    provider: str,
    base_url: str | None = None,
    low_balance_threshold: float = 5,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    """Return explicit uncertainty until a supported balance API is available.

    ``available=None`` means account health could not be established; extraction
    may still be attempted, but callers must report the preflight uncertainty.

    SiliconFlow retired /user/info on 2026-08-14. Its release notes promise a
    future account-level replacement, but none is documented as of 2026-10-14:
    https://api-docs.siliconflow.cn/docs/release-notes/overview

    Do not send keys to guessed endpoints, scrape a private browser session, or
    infer remaining balance from successful inference/model-list requests. Keep
    the threshold argument for a future verified read-only balance integration.
    """
    provider = provider if provider in {"siliconflow", "openai"} else "other"
    result: dict[str, Any] = {
        "status": "health_unknown", "available": None, "low_balance": False,
        "balance": None, "balance_supported": False,
        "reason": "balance_check_unsupported", "provider": provider,
    }
    if not api_key:
        return {**result, "status": "unavailable", "available": False, "reason": "missing_api_key"}
    if provider == "siliconflow":
        return {**result, "reason": "balance_api_retired"}
    return result
