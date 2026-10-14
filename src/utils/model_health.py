"""Local model configuration state; account and balance preflight are disabled."""

from typing import Any


def check_model_health(
    api_key: str | None,
    provider: str,
    base_url: str | None = None,
    low_balance_threshold: float = 5,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    """Check only whether credentials exist, without making a provider request.

    The user disabled advance balance warnings. ``available=None`` is a neutral
    state until an actual extraction succeeds or fails, not a health warning.
    Optional legacy arguments remain accepted for compatibility and are unused.
    """
    provider = provider if provider in {"siliconflow", "openai"} else "other"
    result: dict[str, Any] = {
        "status": "not_checked", "available": None, "low_balance": False,
        "balance": None, "balance_supported": False,
        "reason": "balance_check_disabled", "provider": provider,
    }
    if not api_key:
        return {**result, "status": "unavailable", "available": False, "reason": "missing_api_key"}
    return result
