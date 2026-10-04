"""Loads versioned prompt artifacts from prompts/*.yaml. Rendering never uses str.format on untrusted text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache

import yaml

from app.config import get_settings


@dataclass(frozen=True)
class PromptSpec:
    version: str
    content_hash: str
    system: str
    user_template: str
    max_tokens: int
    styles: dict[str, str]

    def render_user(
        self,
        *,
        title: str,
        source: str,
        text: str,
        style: str,
        max_words: int,
        instructions: str | None,
    ) -> str:
        instructions_block = (
            f"Additional focus requested by the user: {instructions}" if instructions else ""
        )
        out = self.user_template
        for key, value in {
            "{style}": self.styles.get(style, style),
            "{max_words}": str(max_words),
            "{instructions_block}": instructions_block,
            "{title}": title.replace('"', "'"),
            "{source}": source.replace('"', "'"),
            "{text}": text,  # last, so braces inside the document are never re-interpreted
        }.items():
            out = out.replace(key, value)
        return out


@lru_cache
def load_prompt(version: str | None = None) -> PromptSpec:
    settings = get_settings()
    version = version or settings.summarize_prompt_version
    path = settings.prompts_dir / f"{version}.yaml"
    content = path.read_text()
    raw = yaml.safe_load(content)
    return PromptSpec(
        version=str(raw["version"]),
        content_hash=hashlib.sha256(content.encode()).hexdigest()[:12],
        system=raw["system"].strip(),
        user_template=raw["user_template"].strip(),
        max_tokens=int(raw.get("max_tokens", 700)),
        styles=dict(raw.get("styles", {})),
    )
