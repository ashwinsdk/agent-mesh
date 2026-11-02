"""Cost semantics. Never present subscription usage as API spend; never invent provider prices.

billing_mode per CLI comes from mesh.toml [billing]:
  subscription : tokens are reported, cost is "API-equivalent (informational)" and excluded from spend
  api          : reported cost counts as spend; if absent and [prices] has the model, cost is *estimated*
  credits      : provider meters AI credits (Copilot); no currency conversion is attempted
  unknown      : nothing is summed as spend
"""
from __future__ import annotations

MODES = ("subscription", "api", "credits", "unknown")


def billing_mode(cfg: dict, cli: str) -> str:
    m = (cfg.get("billing") or {}).get(cli, "unknown")
    return m if m in MODES else "unknown"


def estimate_cost(cfg: dict, model: str | None, input_t: int | None, output_t: int | None,
                  cache_read: int | None = None) -> float | None:
    """Only if the user configured per-million-token prices: [prices."<model>"] input=..., output=..., cache_read=..."""
    if not model or input_t is None or output_t is None:
        return None
    p = (cfg.get("prices") or {}).get(model)
    if not p or "input" not in p or "output" not in p:
        return None
    cost = input_t / 1e6 * p["input"] + output_t / 1e6 * p["output"]
    if cache_read and "cache_read" in p:
        cost += cache_read / 1e6 * p["cache_read"]
    return round(cost, 6)


def spend_semantics(mode: str, reported: float | None, estimated: float | None) -> dict:
    """What the dashboard may show for one row/aggregate."""
    val, src = (reported, "reported") if reported is not None else (estimated, "estimated") if estimated is not None else (None, "unavailable")
    return {"value": val, "source": src, "counts_as_spend": mode == "api" and val is not None,
            "label": {"subscription": "API-equivalent (informational)", "api": "API spend", "credits": "AI credits (no currency)",
                      "unknown": "unclassified"}[mode]}
