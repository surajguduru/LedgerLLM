"""Versioned price table (config/prices.yaml) and exact integer cost arithmetic in micro-USD."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from app.config import get_settings


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    # Which provider serves the model (app/llm/router.py, D26); None: LLM_PROVIDER.
    provider: str | None = None
    # Hidden-reasoning headroom added to the output cap of a reasoning model (D26).
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class PriceTable:
    version: str
    models: dict[str, ModelPrice]

    def get(self, model: str) -> ModelPrice:
        try:
            return self.models[model]
        except KeyError as exc:
            raise ValueError(
                f"no price for model '{model}' in prices.yaml v{self.version}"
            ) from exc


@lru_cache
def load_prices() -> PriceTable:
    raw = yaml.safe_load(get_settings().prices_path.read_text())
    return PriceTable(
        version=str(raw["version"]),
        models={
            name: ModelPrice(
                float(p["input_usd_per_mtok"]),
                float(p["output_usd_per_mtok"]),
                p.get("provider"),
                int(p.get("reasoning_tokens", 0)),
            )
            for name, p in raw["models"].items()
        },
    )


def compute_cost_microusd(model: str, input_tokens: int, output_tokens: int) -> int:
    """tokens x (USD per 1M tokens) == micro-USD exactly. Rounded once, at the end."""
    price = load_prices().get(model)
    return int(
        round(input_tokens * price.input_usd_per_mtok + output_tokens * price.output_usd_per_mtok)
    )


def estimate_cost_microusd(model: str, est_input_tokens: int, max_output_tokens: int) -> int:
    """Upper bound used for the budget reservation: assumes the model uses every output token."""
    return compute_cost_microusd(model, est_input_tokens, max_output_tokens)
