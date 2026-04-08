"""
pricing.py — Single source of truth for Anthropic API pricing.
https://docs.anthropic.com/en/docs/about-claude/pricing  (April 2026)
Cache write uses the 1-hour rate (2x base input) — Claude Code sessions are long-running.
"""

# Per million tokens
PRICING = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 10.00},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 10.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  6.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  6.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  2.00},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  2.00},
}

_BILLABLE_FAMILIES = ("opus", "sonnet", "haiku")


def is_billable(model: str) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(f in m for f in _BILLABLE_FAMILIES)


def get_pricing(model: str) -> dict | None:
    """Return pricing dict for model, or None if unknown/non-billable."""
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus"   in m: return PRICING["claude-opus-4-6"]
    if "sonnet" in m: return PRICING["claude-sonnet-4-6"]
    if "haiku"  in m: return PRICING["claude-haiku-4-5"]
    return None


def calc_cost(model: str, inp: int, out: int, cache_read: int, cache_creation: int) -> float:
    """Total cost in USD. Returns 0.0 for non-billable or unknown models."""
    if not is_billable(model):
        return 0.0
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )


def calc_cost_breakdown(
    model: str, inp: int, out: int, cache_read: int, cache_creation: int
) -> dict:
    """Per-component costs in USD. All zeros for non-billable/unknown models.

    Returns dict with keys:
        input_cost, output_cost, cache_read_cost, cache_creation_cost,
        cache_savings, cost, billable
    """
    billable = is_billable(model)
    p = get_pricing(model) if billable else None
    if not billable or not p:
        return {
            "input_cost": 0.0, "output_cost": 0.0,
            "cache_read_cost": 0.0, "cache_creation_cost": 0.0,
            "cache_savings": 0.0, "cost": 0.0,
            "billable": False,
        }
    ic = inp            * p["input"]       / 1_000_000
    oc = out            * p["output"]      / 1_000_000
    rc = cache_read     * p["cache_read"]  / 1_000_000
    cc = cache_creation * p["cache_write"] / 1_000_000
    # cache_savings: what you would have paid at full input price minus what you paid
    savings = cache_read * (p["input"] - p["cache_read"]) / 1_000_000
    return {
        "input_cost":          ic,
        "output_cost":         oc,
        "cache_read_cost":     rc,
        "cache_creation_cost": cc,
        "cache_savings":       savings,
        "cost":                ic + oc + rc + cc,
        "billable":            True,
    }
