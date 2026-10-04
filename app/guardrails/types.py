from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class GuardrailVerdict:
    blocked: bool
    category: str | None  # prompt_injection | jailbreak | pii_leak | toxicity | ...
    score: float  # 0..1 confidence that the content is unsafe
    method: str  # heuristic_v1 | llm_classifier | ...
    latency_ms: int = 0
    # Set these when the verdict came from a paid model call. The pipeline then books a
    # usage_ledger row with purpose="guardrail" to the tenant (model must exist in prices.yaml).
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


PASS = GuardrailVerdict(blocked=False, category=None, score=0.0, method="noop")
