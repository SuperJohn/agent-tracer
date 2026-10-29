"""Canonical hashing, illustrative cost, and linear percentiles."""

from __future__ import annotations

import hashlib
import json
import math


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_card: dict,
) -> float:
    """Illustrative dollars from a price card of USD per million tokens.

    Unknown models cost zero. Local Ollama tags are omitted from the card on
    purpose. Rounded to 10 decimal places so the same inputs always match.
    """
    entry = price_card.get(model) or {}
    in_rate = float(entry.get("input_usd_per_million") or 0)
    out_rate = float(entry.get("output_usd_per_million") or 0)
    usd = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    return round(usd, 10)


def percentile(values: list[float], p: float) -> float | None:
    """Linear percentile, the same interpolation numpy uses as 'linear'.

    Rank k = (n - 1) * (p / 100), then interpolate between the surrounding
    sorted samples. One sample returns that sample. Empty returns None.
    """
    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError("percentile p must be between 0 and 100")
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(xs[int(k)])
    weight = k - lo
    return float(xs[lo] + (xs[hi] - xs[lo]) * weight)


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))
