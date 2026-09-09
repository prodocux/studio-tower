from datetime import UTC, datetime
from typing import Dict, Optional, Tuple

PRICING_CATALOG_VERSION = "2026-Q1"
DEFAULT_CURRENCY = "USD"
CATALOG_EFFECTIVE_UNTIL = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

# Exact Model ID Rates in USD per 1,000,000 tokens: (input_per_m, output_per_m, cached_per_m)
# Strict exact matching only (no substring collision)
_EXACT_MODEL_RATES: Dict[str, Tuple[float, float, float]] = {
    "gemini-3.6-flash": (0.10, 0.40, 0.025),
    "gemini-2.5-flash": (0.10, 0.40, 0.025),
    "gemini-2.0-flash": (0.10, 0.40, 0.025),
    "gemini-2.0-flash-exp": (0.10, 0.40, 0.025),
    "gemini-1.5-flash": (0.075, 0.30, 0.01875),
    "gemini-1.5-flash-8b": (0.0375, 0.15, 0.01),
    "gemini-1.5-pro": (1.25, 5.00, 0.3125),
}


def get_model_family(model_id: Optional[str]) -> str:
    """
    Map canonical model identifier to controlled low-cardinality metric label enum.
    """
    if not model_id:
        return "unknown"
    norm = model_id.lower().replace("models/", "").strip()
    if norm.startswith("gemini-3.6-flash"):
        return "gemini-3.6-flash"
    if norm.startswith("gemini-2.5-flash"):
        return "gemini-2.5-flash"
    if norm.startswith("gemini-2.0-flash"):
        return "gemini-2.0-flash"
    if norm.startswith("gemini-1.5-flash"):
        return "gemini-1.5-flash"
    if norm.startswith("gemini-1.5-pro"):
        return "gemini-1.5-pro"
    return "other"


def calculate_estimated_cost(
    model_id: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cached_tokens: Optional[int] = 0,
    catalog_version: Optional[str] = None,
    evaluation_time: Optional[datetime] = None,
) -> Optional[float]:
    """
    Honest, versioned token-based cost estimation with exact model ID matching.
    Returns None if:
    - model_id is not exactly matched in the catalog
    - input_tokens or output_tokens is None
    - catalog_version does not match active version
    - evaluation_time exceeds catalog validity window
    """
    if not model_id or not isinstance(input_tokens, (int, float)) or not isinstance(output_tokens, (int, float)):
        return None

    # Version and validity date guard
    if catalog_version and catalog_version != PRICING_CATALOG_VERSION:
        return None

    eval_dt = evaluation_time or datetime.now(UTC)
    if eval_dt > CATALOG_EFFECTIVE_UNTIL:
        return None

    norm_id = model_id.lower().replace("models/", "").strip()
    rates = _EXACT_MODEL_RATES.get(norm_id)
    if not rates:
        return None

    rate_in, rate_out, rate_cached = rates
    cached = cached_tokens or 0
    billable_in = max(0, input_tokens - cached)

    cost = (
        (billable_in / 1_000_000.0) * rate_in
        + (cached / 1_000_000.0) * rate_cached
        + (output_tokens / 1_000_000.0) * rate_out
    )
    return round(cost, 6)
