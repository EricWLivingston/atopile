"""Identify document type from filename + first-page text (no LLM).

Cheap, deterministic routing so the right type-specific parser/chunker runs. The LLM
fallback for the ~2% of ambiguous docs is intentionally a ``NotImplementedError`` — for
v1 (datasheets only) rename or drop the file under the right ``data/<corpus>/`` folder.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import DocType

DATASHEET_SIGNALS = [
    r"\bdata\s?sheet\b",
    r"\bDS\d{4,}\b",
    r"\bElectrical Characteristics\b",
    r"\b(VCC|VDD|VIN|VOUT)\b.*\b(min|typ|max)\b",
]
STANDARD_SIGNALS = [
    r"\bIPC[-\s]?\d{4}",
    r"\bMIL[-\s]?STD[-\s]?\d+",
    r"\bJESD\d+",
    r"\bIEC\s\d{4,}",
    r"\bISO[/\s]+IEC\s\d{4,}",
]
APP_NOTE_SIGNALS = [
    r"\bapplication note\b",
    r"\bAN[-\s]?\d{2,5}\b",
    r"\bSLVA\d+",  # TI
]


def _read_first_page_text(path: Path, max_chars: int = 8000) -> str:
    """Cheap first-page text via pdfminer.six (no LLM, no network)."""
    try:
        from pdfminer.high_level import extract_text

        return extract_text(str(path), maxpages=1)[:max_chars]
    except Exception:
        return ""


def classify(path: Path) -> DocType:
    """Return the document type for ``path``.

    Resolution order: corpus folder hint → filename pattern → first-page text scan →
    extension default. Raises ``NotImplementedError`` if nothing matches (rename it).
    """
    name = path.stem.lower()

    # 1. Path-based hints (data/<corpus>/...)
    parts_lower = [p.lower() for p in path.parts]
    if "textbooks" in parts_lower:
        return "textbook"
    if "internal" in parts_lower:
        return "internal"
    if "standards" in parts_lower:
        return "standard"
    if "app_notes" in parts_lower:
        return "app_note"
    if "datasheets" in parts_lower:
        return "datasheet"

    # 2. Filename pattern match
    for pattern in STANDARD_SIGNALS:
        if re.search(pattern, name, re.I):
            return "standard"
    for pattern in DATASHEET_SIGNALS:
        if re.search(pattern, name, re.I):
            return "datasheet"

    # 3. First-page text scan (PDFs only)
    if path.suffix.lower() == ".pdf":
        first_page = _read_first_page_text(path)
        for pattern in STANDARD_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "standard"
        for pattern in APP_NOTE_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "app_note"
        for pattern in DATASHEET_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "datasheet"

    # 4. Default for text formats
    if path.suffix.lower() in {".md", ".docx"}:
        return "internal"

    raise NotImplementedError(
        f"Could not classify {path}. Place it under data/<corpus>/ or rename it."
    )
