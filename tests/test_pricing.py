from app.billing.pricing import compute_cost_microusd, estimate_cost_microusd, load_prices
from app.plans import microusd_to_usd, usd_to_microusd


def test_cost_is_exact_integer_microusd():
    # Haiku: $1/MTok in, $5/MTok out -> 3000 in + 300 out = 3000 + 1500 uUSD = $0.0045
    assert compute_cost_microusd("claude-haiku-4-5", 3000, 300) == 4500
    assert microusd_to_usd(4500) == 0.0045


def test_gemini_flash_cost():
    # 3000 in x $0.75/MTok + 300 out x $3.75/MTok = 2250 + 1125 uUSD
    assert compute_cost_microusd("gemini-3.8-flash", 3000, 300) == 3375


def test_groq_eval_models_are_priced():
    # Groq list prices (USD/MTok): qwen3.8-27b 0.80/4.00, gpt-oss-120b 0.15/0.60, gpt-oss-20b 0.075/0.30
    assert compute_cost_microusd("qwen/qwen3.8-27b", 1000, 100) == 800 + 400
    assert compute_cost_microusd("openai/gpt-oss-120b", 10_000, 1000) == 1500 + 600
    assert compute_cost_microusd("openai/gpt-oss-20b", 1000, 1000) == 75 + 300


def test_estimate_is_an_upper_bound_of_actual():
    assert estimate_cost_microusd("claude-haiku-4-5", 1000, 700) >= compute_cost_microusd(
        "claude-haiku-4-5", 1000, 120
    )


def test_price_table_is_versioned():
    assert load_prices().version == "2026-10-08"


def test_usd_roundtrip():
    assert usd_to_microusd(0.5) == 500_000 and microusd_to_usd(500_000) == 0.5
