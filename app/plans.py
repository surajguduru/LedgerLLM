"""Plan tiers loaded from config/plans.yaml. Shared by auth (rpm) and billing (monthly budget)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from app.config import get_settings

MICRO = 1_000_000


def usd_to_microusd(usd: float) -> int:
    return int(round(usd * MICRO))


def microusd_to_usd(microusd: int) -> float:
    return round(microusd / MICRO, 6)


@dataclass(frozen=True)
class Plan:
    name: str
    rpm: int
    monthly_budget_microusd: int
    max_input_chars: int
    allowed_models: tuple[str, ...]


@dataclass(frozen=True)
class PlanCatalog:
    plans: dict[str, Plan]
    soft_warning_fraction: float

    def get(self, name: str) -> Plan:
        try:
            return self.plans[name]
        except KeyError as exc:
            raise ValueError(f"unknown plan '{name}'") from exc


@lru_cache
def load_plans() -> PlanCatalog:
    raw = yaml.safe_load(get_settings().plans_path.read_text())
    plans = {
        name: Plan(
            name=name,
            rpm=int(p["rpm"]),
            monthly_budget_microusd=usd_to_microusd(float(p["monthly_budget_usd"])),
            max_input_chars=int(p["max_input_chars"]),
            allowed_models=tuple(p.get("allowed_models", [])),
        )
        for name, p in raw["plans"].items()
    }
    return PlanCatalog(
        plans=plans, soft_warning_fraction=float(raw.get("soft_warning_fraction", 0.8))
    )


def get_plan(name: str) -> Plan:
    return load_plans().get(name)
