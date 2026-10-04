"""Online quality sampling: judge a small share of live summaries asynchronously.

OWNER: Thrishal. The base ships a no-op hook that the pipeline calls after every successful response. To
implement: sample `QUALITY_SAMPLE_RATE` of requests, run an LLM judge (faithfulness / coverage 1-5, JSON) on a
background thread so the response is never delayed, record `ledgerllm_quality_score{prompt_version}` and a
usage_ledger row with purpose="judge" (platform cost — not settled against the tenant's budget; document the
policy). Also expose the latest sampled scores via an admin endpoint for the dashboard.
"""

from __future__ import annotations


def maybe_sample(
    *, request_id: str, tenant_id: str, prompt_version: str, source_text: str, summary: str
) -> None:
    return None  # STUB — Thrishal
