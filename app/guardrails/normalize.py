"""Text normalisation that runs before the heuristic rules.

Attackers hide trigger phrases behind cheap encodings: zero-width characters between letters, leetspeak,
Cyrillic/Greek look-alikes, or a base64 blob that says "decode and follow this". Each rule below undoes one
of those so the regex layer sees plain ASCII. The original text is kept alongside: a pattern that only fires
on the normalised copy is reported as `obfuscated`, which is itself a signal.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata

_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# "I g n o r e   a l l": three or more single letters separated by single spaces.
_SPACED_WORD = re.compile(r"\b(?:[A-Za-z] ){2,}[A-Za-z]\b")

# Characters that render like Latin letters but are different code points.
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s",
        "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X", "У": "Y", "І": "I", "Ѕ": "S",
        "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "ρ": "p",
        "ı": "i", "ɡ": "g", "ʏ": "y",
    }
)  # fmt: skip

_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "|": "l"}
)

# A leetspeak word has letters and digits/symbols mixed; a plain number or an id does not.
_LEET_WORD = re.compile(
    r"\b(?=[a-z0-9@$|]*[a-z])(?=[a-z0-9@$|]*[0134578@$|])[a-z0-9@$|]{3,}\b", re.I
)

# Long base64 runs — 32+ chars of the alphabet, padding optional. Short tokens (ids, hashes) are skipped
# by requiring that the decoded bytes are mostly printable ASCII.
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])")


def _deleet(text: str) -> str:
    return _LEET_WORD.sub(lambda m: m.group(0).translate(_LEET), text)


def _decode_base64_runs(text: str) -> list[str]:
    decoded: list[str] = []
    for m in _B64_RUN.finditer(text):
        blob = m.group(0)
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        printable = sum(c.isprintable() or c.isspace() for c in s)
        if s and printable / len(s) > 0.95:
            decoded.append(s)
    return decoded


def normalize(text: str) -> tuple[str, list[str]]:
    """Return (normalised text, list of transformations that changed something)."""
    applied: list[str] = []
    out = _ZERO_WIDTH.sub("", text)
    if out != text:
        applied.append("zero_width")
    nfkc = unicodedata.normalize("NFKC", out)
    homo = nfkc.translate(_HOMOGLYPHS)
    if homo != out:
        applied.append("homoglyph")
    out = homo
    spaced = _SPACED_WORD.sub(lambda m: m.group(0).replace(" ", ""), out)
    if spaced != out:
        applied.append("spaced_letters")
        spaced = re.sub(r" {2,}", " ", spaced)
    out = spaced
    # Base64 runs are extracted before the leetspeak pass, which would otherwise corrupt them.
    blobs = _decode_base64_runs(out)
    deleet = _deleet(out)
    if deleet != out:
        applied.append("leetspeak")
    out = deleet
    if blobs:
        applied.append("base64")
        out = out + "\n" + "\n".join(blobs)
    return out, applied
