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
    cache_ttl_s: int = 0  # response cache lifetime; 0 = cache off for this plan
    default_model: str | None = None  # used when a request names no model; else DEFAULT_MODEL


@dataclass(frozen=True)
class PlanCatalog:
    plans: dict[str, Plan]
    soft_warning_fraction: float

    def get(self, name: str) -> Plan:
        try:
            return self.plans[name]
        except KeyError as exc:
            raise ValueError(f"unknown plan '{name}'") from exc


def parse_plan(name: str, p: dict) -> Plan:
    plan = Plan(
        name=name,
        rpm=int(p["rpm"]),
        monthly_budget_microusd=usd_to_microusd(float(p["monthly_budget_usd"])),
        max_input_chars=int(p["max_input_chars"]),
        allowed_models=tuple(p.get("allowed_models", [])),
        cache_ttl_s=int(p.get("cache_ttl_s", 0)),
        default_model=p.get("default_model"),
    )
    if plan.default_model and plan.default_model not in plan.allowed_models:
        raise ValueError(
            f"plan '{name}': default_model '{plan.default_model}' is not in allowed_models"
        )
    return plan


@lru_cache
def load_plans() -> PlanCatalog:
    raw = yaml.safe_load(get_settings().plans_path.read_text())
    plans = {name: parse_plan(name, p) for name, p in raw["plans"].items()}
    return PlanCatalog(
        plans=plans, soft_warning_fraction=float(raw.get("soft_warning_fraction", 0.8))
    )


def get_plan(name: str) -> Plan:
    return load_plans().get(name)
