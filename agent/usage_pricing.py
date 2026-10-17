"""Token accounting and cost estimation.

Pricing comes only from the endpoint's own OpenAI-compatible ``/models``
listing (a ``pricing`` block per model); without one the cost is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Literal, Optional

from agent.model_metadata import fetch_endpoint_model_metadata
from utils import base_url_hostname


_ZERO = Decimal("0")
_ONE_MILLION = Decimal("1000000")

# Sub-cent cost threshold: below $0.01, render at 4 decimal places so
# the display is non-zero (e.g. $0.0046 instead of $0.00).
_SUBCENT_THRESHOLD = Decimal("0.01")

def format_cost_label(amount: Decimal) -> str:
    """Format a cost amount as a display label.

    Scales precision to magnitude:
    - Zero → "$0.00"
    - Sub-cent (< $0.01) → "~$0.0046" (4 dp; amounts that ROUND to
      0.0000 at 4 dp — i.e. at or below $0.00005 under banker's
      rounding — fall back to "~$<0.0001" so the label never reads
      as zero)
    - Normal → "~$1.23" (2 dp)

    Shared by per-response cost labels (estimate_usage_cost) and the
    insights cost-bucket formatters — keep both surfaces on this one
    implementation so sub-cent honesty can't regress on one of them.
    """
    if amount == _ZERO:
        return "$0.00"
    if amount < _SUBCENT_THRESHOLD:
        label = f"~${amount:.4f}"
        # A positive amount that rounds to 0.0000 at 4 dp would render
        # "~$0.0000" — a zero-looking label. Comparing the rendered label
        # checks the truth directly (a naive `< 0.00005` threshold misses
        # the exact boundary under ROUND_HALF_EVEN).
        return label if label != "~$0.0000" else "~$<0.0001"
    return f"~${amount:.2f}"

CostStatus = Literal["actual", "estimated", "included", "unknown"]
CostSource = Literal["provider_models_api", "none"]


@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw_usage: Optional[dict[str, Any]] = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def __add__(self, other: "CanonicalUsage") -> "CanonicalUsage":
        """Sum two usage buckets.

        ``raw_usage`` is dropped on the sum — it describes a single API
        response and cannot be meaningfully merged. ``request_count`` adds so
        callers can see how many underlying API calls a combined figure covers.
        """
        if not isinstance(other, CanonicalUsage):
            return NotImplemented
        return CanonicalUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            request_count=self.request_count + other.request_count,
            raw_usage=None,
        )


@dataclass(frozen=True)
class BillingRoute:
    provider: str
    model: str
    base_url: str = ""
    billing_mode: str = "unknown"


@dataclass(frozen=True)
class PricingEntry:
    input_cost_per_million: Optional[Decimal] = None
    output_cost_per_million: Optional[Decimal] = None
    cache_read_cost_per_million: Optional[Decimal] = None
    cache_write_cost_per_million: Optional[Decimal] = None
    request_cost: Optional[Decimal] = None
    source: CostSource = "none"
    source_url: Optional[str] = None
    pricing_version: Optional[str] = None
    fetched_at: Optional[datetime] = None


@dataclass(frozen=True)
class CostResult:
    amount_usd: Optional[Decimal]
    status: CostStatus
    source: CostSource
    label: str
    fetched_at: Optional[datetime] = None
    pricing_version: Optional[str] = None
    notes: tuple[str, ...] = ()


_UTC_NOW = lambda: datetime.now(timezone.utc)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _usage_get(obj: Any, name: str, default: Any = 0) -> Any:
    """Read a field from a usage object that may be a dict or an attribute object.

    Usage arrives either as a typed SDK object (accessible via ``getattr``)
    or a plain ``dict`` (from JSON deserialisation).  Using ``getattr`` on a
    dict silently yields the default, zeroing out all token counts.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_count(value: Any) -> int:
    """Coerce a usage counter to a non-negative integer.

    Providers occasionally emit malformed negative counters; clamp them to 0
    so a bad field cannot corrupt session accounting.
    """
    return max(0, _to_int(value))


def resolve_billing_route(
    model_name: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> BillingRoute:
    provider_name = (provider or "").strip().lower()
    base = (base_url or "").strip().lower()
    model = (model_name or "").strip()
    if provider_name in {"custom", "local"} or (base and base_url_hostname(base) in ("localhost", "127.0.0.1")):
        return BillingRoute(provider=provider_name or "custom", model=model, base_url=base_url or "", billing_mode="unknown")
    return BillingRoute(provider=provider_name or "unknown", model=model.split("/")[-1] if model else "", base_url=base_url or "", billing_mode="unknown")


def _pricing_entry_from_metadata(
    metadata: Dict[str, Dict[str, Any]],
    model_id: str,
    *,
    source_url: str,
    pricing_version: str,
) -> Optional[PricingEntry]:
    if model_id not in metadata:
        return None
    pricing = metadata[model_id].get("pricing") or {}
    prompt = _to_decimal(pricing.get("prompt"))
    completion = _to_decimal(pricing.get("completion"))
    request = _to_decimal(pricing.get("request"))
    cache_read = _to_decimal(
        pricing.get("cache_read")
        or pricing.get("cached_prompt")
        or pricing.get("input_cache_read")
    )
    cache_write = _to_decimal(
        pricing.get("cache_write")
        or pricing.get("cache_creation")
        or pricing.get("input_cache_write")
    )
    if prompt is None and completion is None and request is None:
        return None

    def _per_token_to_per_million(value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None:
            return None
        return value * _ONE_MILLION

    return PricingEntry(
        input_cost_per_million=_per_token_to_per_million(prompt),
        output_cost_per_million=_per_token_to_per_million(completion),
        cache_read_cost_per_million=_per_token_to_per_million(cache_read),
        cache_write_cost_per_million=_per_token_to_per_million(cache_write),
        request_cost=request,
        source="provider_models_api",
        source_url=source_url,
        pricing_version=pricing_version,
        fetched_at=_UTC_NOW(),
    )


def get_pricing_entry(
    model_name: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[PricingEntry]:
    route = resolve_billing_route(model_name, provider=provider, base_url=base_url)
    if not route.base_url:
        return None
    return _pricing_entry_from_metadata(
        fetch_endpoint_model_metadata(route.base_url, api_key=api_key or ""),
        route.model,
        source_url=f"{route.base_url.rstrip('/')}/models",
        pricing_version="openai-compatible-models-api",
    )


def normalize_usage(
    response_usage: Any,
    *,
    provider: Optional[str] = None,
    api_mode: Optional[str] = None,
) -> CanonicalUsage:
    """Normalize raw Chat Completions usage into canonical token buckets.

    ``prompt_tokens`` includes cache tokens; ``prompt_tokens_details``
    separates them, so input_tokens is derived by subtracting cache tokens
    from the total.
    """
    if not response_usage:
        return CanonicalUsage()

    # OpenAI-style names first; fall back to Anthropic-style
    # (input_tokens/output_tokens). Local OpenAI-compatible servers like
    # mlx_vlm.server emit the Anthropic names in chat_completions responses,
    # and the OpenAI Python client preserves them as extra attributes.
    prompt_total = _usage_count(
        _usage_get(response_usage, "prompt_tokens", 0)
    ) or _usage_count(_usage_get(response_usage, "input_tokens", 0))
    output_tokens = _usage_count(
        _usage_get(response_usage, "completion_tokens", 0)
    ) or _usage_count(_usage_get(response_usage, "output_tokens", 0))
    details = _usage_get(response_usage, "prompt_tokens_details", None)
    # Primary: OpenAI-style prompt_tokens_details. Fallback: Anthropic-style
    # top-level fields that some OpenAI-compatible proxies expose — without
    # this fallback, cache writes are undercounted as 0 and cache reads can be
    # missed when the proxy only surfaces them at the top level.
    cache_read_tokens = _usage_count(
        _usage_get(details, "cached_tokens", 0) if details else 0
    )
    if not cache_read_tokens:
        cache_read_tokens = _usage_count(
            _usage_get(response_usage, "cache_read_input_tokens", 0)
        )
    if not cache_read_tokens:
        # DeepSeek's native API reports context-cache hits as top-level
        # prompt_cache_hit_tokens (+ the complementary
        # prompt_cache_miss_tokens; prompt_tokens = hit + miss), not the
        # OpenAI nested shape.
        cache_read_tokens = _usage_count(
            _usage_get(response_usage, "prompt_cache_hit_tokens", 0)
        )
    if not cache_read_tokens:
        # Some servers report cache hits as a top-level usage.cached_tokens.
        cache_read_tokens = _usage_count(
            _usage_get(response_usage, "cached_tokens", 0)
        )
    cache_write_tokens = _usage_count(
        _usage_get(details, "cache_write_tokens", 0) if details else 0
    )
    if not cache_write_tokens:
        cache_write_tokens = _usage_count(
            _usage_get(details, "cache_creation_input_tokens", 0)
            if details else 0
        )
    if not cache_write_tokens:
        cache_write_tokens = _usage_count(
            _usage_get(response_usage, "cache_creation_input_tokens", 0)
        )
    if not cache_write_tokens:
        cache_write_tokens = _usage_count(
            _usage_get(response_usage, "cache_write_tokens", 0)
        )
    input_tokens = max(0, prompt_total - cache_read_tokens - cache_write_tokens)

    reasoning_tokens = 0
    # Responses-style output_tokens_details.reasoning_tokens, then the Chat
    # Completions completion_tokens_details.reasoning_tokens — reading only the
    # former left hidden thinking invisible in session accounting.
    output_details = _usage_get(response_usage, "output_tokens_details", None)
    if output_details:
        reasoning_tokens = _usage_count(_usage_get(output_details, "reasoning_tokens", 0))
    if not reasoning_tokens:
        completion_details = _usage_get(response_usage, "completion_tokens_details", None)
        if completion_details:
            reasoning_tokens = _usage_count(
                _usage_get(completion_details, "reasoning_tokens", 0)
            )

    return CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def estimate_usage_cost(
    model_name: str,
    usage: CanonicalUsage,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> CostResult:
    entry = get_pricing_entry(model_name, provider=provider, base_url=base_url, api_key=api_key)
    if not entry:
        return CostResult(amount_usd=None, status="unknown", source="none", label="n/a")

    amount = _ZERO

    if usage.input_tokens and entry.input_cost_per_million is None:
        return CostResult(amount_usd=None, status="unknown", source=entry.source, label="n/a")
    if usage.output_tokens and entry.output_cost_per_million is None:
        return CostResult(amount_usd=None, status="unknown", source=entry.source, label="n/a")
    if usage.cache_read_tokens:
        if entry.cache_read_cost_per_million is None:
            return CostResult(
                amount_usd=None,
                status="unknown",
                source=entry.source,
                label="n/a",
                notes=("cache-read pricing unavailable for route",),
            )
    if usage.cache_write_tokens:
        if entry.cache_write_cost_per_million is None:
            return CostResult(
                amount_usd=None,
                status="unknown",
                source=entry.source,
                label="n/a",
                notes=("cache-write pricing unavailable for route",),
            )

    if entry.input_cost_per_million is not None:
        amount += Decimal(usage.input_tokens) * entry.input_cost_per_million / _ONE_MILLION
    if entry.output_cost_per_million is not None:
        amount += Decimal(usage.output_tokens) * entry.output_cost_per_million / _ONE_MILLION
    if entry.cache_read_cost_per_million is not None:
        amount += Decimal(usage.cache_read_tokens) * entry.cache_read_cost_per_million / _ONE_MILLION
    if entry.cache_write_cost_per_million is not None:
        amount += Decimal(usage.cache_write_tokens) * entry.cache_write_cost_per_million / _ONE_MILLION
    if entry.request_cost is not None and usage.request_count:
        amount += Decimal(usage.request_count) * entry.request_cost

    return CostResult(
        amount_usd=amount,
        status="estimated",
        source=entry.source,
        label=format_cost_label(amount),
        fetched_at=entry.fetched_at,
        pricing_version=entry.pricing_version,
    )


def has_known_pricing(
    model_name: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Check whether we have pricing data for this model+route."""
    return get_pricing_entry(model_name, provider=provider, base_url=base_url, api_key=api_key) is not None


def format_duration_compact(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"
