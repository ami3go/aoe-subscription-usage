"""Small explicit Korean/English catalog; no process-wide locale changes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MESSAGES = json.loads((Path(__file__).parent / "locales/en.json").read_text(encoding="utf-8"))
_language = "ko"


def set_language(value: Any) -> None:
    global _language
    _language = value if value in ("en", "ko") else "ko"


def get_language() -> str:
    return _language


def t(source: str, **values: Any) -> str:
    text = MESSAGES.get(source, source) if _language == "en" else source
    return text.format(**values) if values else text


def weekday(index: int) -> str:
    return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[index] if _language == "en" else "월화수목금토일"[index]
