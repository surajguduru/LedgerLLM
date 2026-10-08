"""Input guardrail: prompt-injection / jailbreak detection on user instructions AND fetched content.

Layer 1 (this module): weighted regex *signals*. Each rule contributes a weight; the verdict score is the
noisy-OR of everything that fired, `1 - Π(1 - w)`. One strong phrase ("ignore all previous instructions")
is enough to block; a bare keyword ("jailbreak" in a security article) only raises the score and needs
corroboration. Text is normalised first (app/guardrails/normalize.py) so zero-width characters, leetspeak,
homoglyphs and base64 blobs do not hide a phrase; a hit that needed normalisation adds an `obfuscation`
signal of its own.

Layer 2 (app/guardrails/llm_classifier.py): when `GUARDRAIL_LLM=on` and the heuristic score lands in the
uncertain band, a small JSON classifier decides and its tokens are returned on the verdict so the pipeline
bills them to the tenant.

Thresholds differ by source: fetched documents are more likely to contain trigger words innocently (news
about jailbreaks, docs about system prompts), so they need a higher score than user instructions.

Verdicts for `source="document"` are indirect-injection signals; the prompt also treats the document as data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter

from app.compliance.redaction import redact
from app.guardrails import llm_classifier
from app.guardrails.normalize import normalize
from app.guardrails.types import GuardrailVerdict

METHOD = "heuristic_v2"
CASCADE_METHOD = "cascade_v1"  # heuristics + LLM classifier

# Block when the combined score reaches this. Documents get more slack than instructions.
THRESHOLDS: dict[str, float] = {"instructions": 0.8, "document": 0.9}

# Heuristic scores inside this band are "uncertain": the LLM layer is consulted when enabled.
UNCERTAIN_BAND = (0.3, 0.9)


@dataclass(frozen=True)
class Signal:
    name: str
    category: str  # prompt_injection | jailbreak
    weight: float
    pattern: re.Pattern[str]


def _s(name: str, category: str, weight: float, regex: str, flags: int = re.I) -> Signal:
    return Signal(name, category, weight, re.compile(regex, flags))


_INSTRUCTION_NOUN = r"(instructions?|prompts?|rules?|guidelines?|directions?|commands?|directives?|constraints?|context|programming|training)"

SIGNALS: list[Signal] = [
    # --- direct overrides (strong on their own) -------------------------------------------------
    _s(
        "override_instructions",
        "prompt_injection",
        0.9,
        r"\b(ignore|disregard|forget|override|bypass|skip|discard|drop|abandon)\s+"
        r"(all\s+|any\s+|the\s+|your\s+|my\s+|every\s+|of\s+your\s+|whatever\s+)?"
        r"(previous|prior|above|earlier|preceding|original|initial|system|existing|other|former|given|these)\s+"
        r"(\w+\s+)?" + _INSTRUCTION_NOUN,
    ),
    _s(
        "reveal_system_prompt",
        "prompt_injection",
        0.9,
        r"\b(reveal|print|show|repeat|leak|output|display|dump|disclose|expose|recite|paste|echo|append|attach|copy|quote|tell\s+me|what\s+(is|are|was))\s+"
        r"(me\s+)?(your\s+|the\s+|all\s+|its\s+|any\s+|of\s+your\s+)?(full\s+|complete\s+|exact\s+|entire\s+|original\s+|hidden\s+|secret\s+)?"
        r"(system\s+prompt|hidden\s+instructions?|initial\s+prompt|secret\s+instructions?|developer\s+message|"
        r"original\s+instructions?|internal\s+rules|system\s+message|system\s+instructions?|configuration\s+prompt|instructions\s+above)",
    ),
    _s(
        "persona_switch",
        "jailbreak",
        0.9,
        r"\byou\s+are\s+(now\s+|going\s+to\s+be\s+|henceforth\s+)?(DAN|AIM|STAN|DUDE|an?\s+unrestricted|unfiltered|unrestricted|"
        r"in\s+developer\s+mode|free\s+of\s+(all\s+)?(rules|restrictions)|an?\s+ai\s+(with\s+no|without)|evil|uncensored|jailbroken)\b",
    ),
    _s(
        "fake_system_block",
        "prompt_injection",
        0.85,
        r"(^|[\n\[\(\-#*>|]|\s)(system|assistant|developer|admin|root)\s*(:|\]|>|prompt\s*:|message\s*:)\s*"
        r"(you\s+(are|must|will|should)|ignore|disregard|new\s+instructions|override|from\s+now)",
    ),
    _s(
        "no_rules_persona",
        "jailbreak",
        0.85,
        r"\b(pretend|act|behave|imagine|assume|roleplay|respond)\s+(that\s+|as\s+if\s+|like\s+|as\s+)?(you\s+|an?\s+(ai|model|assistant)\s+(that|which|who)\s+)?"
        r"(have|are|had|were|has|is|with)\s+(no|without|zero|not\s+bound\s+by|free\s+of)\s+"
        r"(any\s+)?(rules|restrictions|guidelines|limits|filters|limitations|content\s+policy|ethical|ethics|safety|guardrails|morals)",
    ),
    _s(
        "jailbreak_verb",
        "jailbreak",
        0.85,
        r"\bjailbreak(ing|ed)?\s+(the|this|your|yourself|you|it|my|an?)\b",
    ),
    _s(
        "privilege_claim",
        "prompt_injection",
        0.7,
        r"\b(admin|developer|debug|maintenance|override|sudo|root)\s+(mode|access|override|privileges?)\s+"
        r"(is\s+|has\s+been\s+|are\s+)?(enabled|activated|granted|unlocked|active|engaged)\b",
    ),
    # --- medium signals: need corroboration ------------------------------------------------------
    _s(
        "developer_mode",
        "jailbreak",
        0.75,
        r"\b(developer|god|dan|jailbreak|unrestricted|unfiltered|uncensored|evil|do[- ]anything[- ]now)\s+mode\b",
    ),
    _s(
        "disable_safety",
        "jailbreak",
        0.7,
        r"\b(no|without|disable|bypass|turn\s+off|remove|drop|suspend|lift|has\s+no)\s+(the\s+|your\s+|any\s+|all\s+|its\s+)?"
        r"(content\s+policy|safety\s+(filters?|guidelines?|rules|measures|training)|restrictions|guardrails|filters|moderation|"
        r"ethical\s+guidelines|censorship|limitations)\b",
    ),
    _s(
        "respond_only_with",
        "prompt_injection",
        0.7,
        r"\b(respond|reply|answer|output|say)\s+(only\s+|solely\s+|exclusively\s+)?with\s+(only\s+|just\s+)?"
        r"(the\s+(word|phrase|text|string|following|sentence|message)|exactly|just|nothing\s+but|['\"“])",
    ),
    _s(
        "new_instructions_header",
        "prompt_injection",
        0.7,
        r"\b(new|updated|real|actual|true|important|urgent)\s+(system\s+)?(instructions?|rules?|directives?|task|prompt)\s*[:\-]",
    ),
    _s(
        "canary_token",
        "prompt_injection",
        0.7,
        r"\b(pwned|i\s+have\s+been\s+hacked|ai\s+injection\s+succeeded|injection\s+successful)\b",
    ),
    _s(
        "address_the_ai",
        "prompt_injection",
        0.6,
        r"\b(to|for|note\s+to|attention|dear|hey|hi|hello|psst|message\s+(to|for)|instructions?\s+(to|for))[,\s]+"
        r"(the\s+|any\s+|all\s+|every\s+|whatever\s+)?(ai|assistant|llm|language\s+model|model|chatbot|gpt|claude|gemini|summarizer|bot|agent)s?\b"
        r"\s*(reading|processing|summarizing|summarising|that\s+(reads|is\s+reading|processes)|which\s+reads|:|,|-|—)",
    ),
    _s(
        "ai_marker",
        "prompt_injection",
        0.6,
        r"\b(ai|assistant|llm|model|chatbot)\s+(instructions?|directive|note|override|command)s?\s*:",
    ),
    _s(
        "instruct_summary_content",
        "prompt_injection",
        0.6,
        r"\b(in\s+(your|the)\s+(summary|response|answer|output)|(your|the)\s+summary\s+(must|should|will|needs\s+to|has\s+to)|when\s+(you\s+)?summari[sz]ing)"
        r"(?:(?!\.\s)[^\n]){0,80}\b(include|say|write|add|mention|insert|contain|state|recommend|link|end\s+with|start\s+with)\b",
    ),
    _s(
        "instruct_summary_content_rev",
        "prompt_injection",
        0.6,
        r"\b(include|add|insert|put|mention|embed|place|append)\b(?:(?!\.\s)[^\n]){0,80}\b(in|to|into)\s+(your|the)\s+(summary|response|answer|output)\b",
    ),
    _s(
        "stop_summarizing",
        "prompt_injection",
        0.6,
        r"\b(do\s+not|don'?t|stop|never|instead\s+of|rather\s+than|forget\s+about)\s+(summari[sz]|the\s+summary|your\s+(task|job))",
    ),
    _s(
        "instead_do",
        "prompt_injection",
        0.5,
        r"\binstead,?\s+(just\s+|please\s+|simply\s+)?(write|say|output|respond|reply|print|tell|translate|generate|return|produce|reveal)\b",
    ),
    _s(
        "authority_claim",
        "prompt_injection",
        0.55,
        r"\b(i\s+am|i'm|this\s+is|we\s+are)\s+(your|the)\s+(developer|creator|admin(istrator)?|operator|owner|engineer|openai|anthropic|google)\b",
    ),
    _s(
        "from_now_on",
        "prompt_injection",
        0.5,
        r"\b(from\s+now\s+on|henceforth|for\s+the\s+rest\s+of\s+(this|the)\s+(conversation|session)|starting\s+now|going\s+forward)\b",
    ),
    _s(
        "regardless_of_input",
        "prompt_injection",
        0.5,
        r"\bregardless\s+of\s+(what\s+)?(the|any|your|its)\s+(document|instructions|text|content|rules|input|source|article|user)\b",
    ),
    _s(
        "include_literal",
        "prompt_injection",
        0.45,
        r"\b(include|insert|add|append|put|write|say|print|emit|output)\s+(the\s+|this\s+|these\s+)?(exact\s+)?((phrase|text|words?|string|sentence|link|url|line)\s*)?['\"“]",
    ),
    _s(
        "hidden_markup_instruction",
        "prompt_injection",
        0.5,
        r"<!--[^>]{0,200}\b(ignore|instruction|assistant|ai|system|summar|respond|reply|output)\b[^>]{0,200}-->",
    ),
    _s(
        "hidden_css_text",
        "prompt_injection",
        0.5,
        r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|color\s*:\s*(white|#fff)|opacity\s*:\s*0)[^<]{0,80}>[^<]{0,200}\b(ignore|instruction|assistant|ai|system|summar)\b",
    ),
    _s(
        "decode_and_follow",
        "prompt_injection",
        0.6,
        r"\b(decode|base64[- ]?decode|unscramble|read\s+backwards|rot13|decrypt|translate)\b[^.\n]{0,60}\b(and\s+)?(follow|execute|obey|do|apply|run|comply)\b",
    ),
    _s(
        "instruct_user_action",
        "prompt_injection",
        0.6,
        r"\btell\s+the\s+(user|reader|human)s?\s+to\s+(click|visit|call|buy|download|install|open|send|go\s+to)\b",
    ),
    _s(
        "persona_as_uncensored",
        "jailbreak",
        0.85,
        r"\b(roleplay|role-play|act|respond|behave|answer|reply)\s+as\s+(an?\s+)?(uncensored|unfiltered|unrestricted|jailbroken|evil|unaligned|rogue)\s+(ai|model|assistant|chatbot|bot|version|llm)\b",
    ),
    _s(
        "ignores_training",
        "jailbreak",
        0.6,
        r"\b(ignores?|ignoring|without|forget(s|ting)?)\s+(its|your|their|all)\s+(training|guidelines|safety|alignment|rules)\b",
    ),
    _s(
        "jailbreak_keyword",
        "jailbreak",
        0.35,
        r"\bjailbreak",
    ),
    _s(
        "system_prompt_keyword",
        "prompt_injection",
        0.2,
        r"\bsystem\s+prompt\b",
    ),
    # --- multilingual direct overrides ----------------------------------------------------------
    _s(
        "override_es",
        "prompt_injection",
        0.9,
        r"\b(ignora|ignore|olvida|omite|descarta)\s+(todas?\s+)?(las?\s+|tus\s+)?(instrucciones|reglas|indicaciones)\s+(anteriores|previas|del\s+sistema)",
    ),
    _s(
        "override_fr",
        "prompt_injection",
        0.9,
        r"\b(ignore[zr]?|oublie[zr]?|néglige[zr]?)\s+(toutes\s+)?(les\s+|tes\s+|vos\s+)?(instructions|règles|consignes)\s+(précédentes|precedentes|antérieures|anterieures|du\s+système|systeme)",
    ),
    _s(
        "override_de",
        "prompt_injection",
        0.9,
        r"\b(ignorier(e|en|st)?|vergiss|missachte)\s+(alle\s+|die\s+|deine\s+)?(vorherigen|bisherigen|obigen|früheren|fruheren|vorigen)\s+(anweisungen|befehle|regeln|instruktionen)",
    ),
    _s(
        "override_pt_it",
        "prompt_injection",
        0.9,
        r"\b(ignore|ignora|esqueça|dimentica)\s+(todas?\s+|tutte\s+)?(as\s+|le\s+)?(instruções|instrucoes|istruzioni|regras|regole)\s+(anteriores|precedenti|prévias)",
    ),
    _s(
        "override_ru",
        "prompt_injection",
        0.9,
        r"(игнорируй(те)?|забудь(те)?|проигнорируй(те)?)\s+(все\s+)?(предыдущие|прежние|вышеуказанные)\s+(инструкции|указания|правила)",
    ),
    _s(
        "override_zh",
        "prompt_injection",
        0.9,
        r"(忽略|无视|忘记)(之前|以上|所有|先前|上面|前面)的?(所有)?(指令|指示|说明|规则)",
    ),
    _s(
        "override_ja",
        "prompt_injection",
        0.9,
        r"(以前|これまで|上記|前)の(指示|命令|ルール)を(無視|忘れ)",
    ),
    _s(
        "override_hi",
        "prompt_injection",
        0.9,
        r"(पिछले|पहले\s+के|ऊपर\s+के)\s+(सभी\s+)?(निर्देशों|निर्देश|नियमों)\s+को\s+(अनदेखा|नज़रअंदाज़|भूल)",
    ),
    _s(
        "override_hinglish",
        "prompt_injection",
        0.9,
        r"\b(pichli|pichle|pehle\s+(ki|ke|wali)|purani|saari|upar\s+(ki|wali))\s+(sab\s+|saari\s+)?(instructions?|rules?|hidayat|niyam)\s+(ko\s+)?(ignore|bhool|chhod|chod)",
    ),
    _s(
        "reveal_es_fr",
        "prompt_injection",
        0.9,
        r"\b(muestra|muéstrame|revela|imprime|révèle|revele|affiche|montre|zeige|verrate)\s+(moi\s+|mir\s+)?(el\s+|le\s+|das\s+|den\s+|tu\s+|ton\s+|dein\s+|deinen\s+)?"
        r"(prompt|mensaje|message|system[- ]?prompt|systemprompt)\s*(del?\s+sistema|système|systeme|du\s+système)?",
    ),
]

OBFUSCATION_WEIGHT = 0.4
_MAX_SNIPPET = 60


def _fire(text: str) -> dict[str, tuple[Signal, str]]:
    hits: dict[str, tuple[Signal, str]] = {}
    for sig in SIGNALS:
        m = sig.pattern.search(text)
        if m:
            hits[sig.name] = (sig, m.group(0)[:_MAX_SNIPPET])
    return hits


def _noisy_or(weights: list[float]) -> float:
    p = 1.0
    for w in weights:
        p *= 1.0 - w
    return round(1.0 - p, 3)


def heuristic_score(text: str) -> tuple[float, str | None, list[dict]]:
    """Score `text` with the regex signals. Returns (score, dominant category, fired signals)."""
    if not text:
        return 0.0, None, []
    raw_hits = _fire(text)
    normalized, applied = normalize(text)
    hits = dict(raw_hits)
    if applied:
        for name, hit in _fire(normalized).items():
            hits.setdefault(name, hit)
    fired = [
        {"name": name, "weight": sig.weight, "match": redact(snippet).text}
        for name, (sig, snippet) in hits.items()
    ]
    if not fired:
        return 0.0, None, []
    weights = [f["weight"] for f in fired]
    # A phrase that only surfaced after de-obfuscation is itself suspicious: nobody leetspeaks a
    # legitimate instruction.
    if applied and len(hits) > len(raw_hits):
        fired.append(
            {"name": "obfuscation", "weight": OBFUSCATION_WEIGHT, "match": ",".join(applied)}
        )
        weights.append(OBFUSCATION_WEIGHT)
    fired.sort(key=lambda f: -f["weight"])
    category = (
        hits[fired[0]["name"]][0].category if fired[0]["name"] in hits else "prompt_injection"
    )
    return _noisy_or(weights), category, fired


def classify_input(text: str, *, source: str = "instructions") -> GuardrailVerdict:
    t0 = perf_counter()
    threshold = THRESHOLDS.get(source, THRESHOLDS["instructions"])
    score, category, signals = heuristic_score(text)
    details: dict = {
        "source": source,
        "threshold": threshold,
        "heuristic_score": score,
        "heuristic_category": category,
        "signals": signals,
    }
    method = METHOD
    model, input_tokens, output_tokens = None, 0, 0

    lo, hi = UNCERTAIN_BAND
    if lo <= score < hi and llm_classifier.enabled():
        llm = llm_classifier.classify_with_llm(text, source=source)
        if llm is not None:
            method = CASCADE_METHOD
            details["llm"] = {
                "injection": llm.injection,
                "category": llm.category,
                "confidence": llm.confidence,
                "latency_ms": llm.latency_ms,
                "cached": llm.cached,
            }
            # The classifier is the better judge inside the band: an "injection" answer lifts the
            # score to its confidence, a "clean" answer caps it at (1 - confidence).
            if llm.injection:
                score = max(score, llm.confidence)
                category = llm.category
            else:
                score = min(score, round(1.0 - llm.confidence, 3))
            # Cached verdicts cost nothing: no model/tokens, so the pipeline books no ledger row.
            if not llm.cached:
                model, input_tokens, output_tokens = llm.model, llm.input_tokens, llm.output_tokens

    return GuardrailVerdict(
        blocked=score >= threshold,
        category=category if score >= threshold else None,
        score=score,
        method=method,
        latency_ms=int((perf_counter() - t0) * 1000),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        details=details,
    )
