"""Attach metadata (+ optional summaries) to raw chunks before embedding/storage.

Deterministic ``chunk_id`` keeps citations stable across re-ingests. MPN/manufacturer
are pulled by regex for datasheet filtering. The one-sentence ``summary`` is optional
(off by default; see ``config.SUMMARIES_ENABLED``) and uses OpenAI chat directly — no
LangChain.
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .chunk import RawChunk
from .config import SUMMARIES_ENABLED, SUMMARY_MODEL

# Known MPN patterns -> manufacturer. Extend as new vendors show up.
MPN_PATTERNS = [
    (r"\b(STM32[A-Z]\d+[A-Z0-9]+)\b", "STMicroelectronics"),
    (r"\b(nRF\d{5}-?[A-Z]*-?R?\d?)\b", "Nordic Semiconductor"),
    (r"\b(TLV\d{3,4}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(TPS\d{3,5}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(LM\d{2,4}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(LT\d{4}[A-Z]?(?:-\d+)?)\b", "Analog Devices / Linear"),
    (r"\b(MAX\d{3,5}[A-Z0-9-]*)\b", "Analog Devices / Maxim"),
    (r"\b(ATmega\d+[A-Z0-9-]*)\b", "Microchip / Atmel"),
    (r"\b(ESP32-[A-Z0-9-]+)\b", "Espressif"),
]

SUMMARY_PROMPT = """\
Summarize this engineering document chunk in ONE sentence (<=25 words).
Focus on the specific fact, parameter, rule, or design pattern it covers.
Be concrete — name the parameter, threshold, or technique, not "discusses X".

Chunk:
{content}

One-sentence summary:"""


def extract_mpn_and_manufacturer(text: str) -> tuple[str | None, str | None]:
    for pattern, manufacturer in MPN_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1), manufacturer
    return None, None


def stable_chunk_id(source_hash: str, section_path: str, content: str) -> str:
    """Stable across re-ingests if the section + first 200 chars are unchanged."""
    composite = f"{source_hash}|{section_path}|{content[:200]}"
    return hashlib.sha256(composite.encode()).hexdigest()[:24]


def _summarize_one(content: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=80,
        temperature=0,
        messages=[
            {"role": "user", "content": SUMMARY_PROMPT.format(content=content[:2000])}
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _summaries(contents: list[str], enabled: bool) -> list[str | None]:
    if not enabled:
        return [None] * len(contents)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_summarize_one, contents))


def enrich(
    raw_chunks: list[RawChunk],
    *,
    corpus: str,
    source_path: str,
    source_hash: str,
    doc_type: str,
    with_summaries: bool | None = None,
) -> list[dict]:
    """Return enriched ``{content, metadata}`` dicts ready for embed + store."""
    if with_summaries is None:
        with_summaries = SUMMARIES_ENABLED
    summaries = _summaries([c.content for c in raw_chunks], with_summaries)

    enriched: list[dict] = []
    for raw, summary in zip(raw_chunks, summaries):
        metadata: dict = {
            "chunk_id": stable_chunk_id(source_hash, raw.section_path, raw.content),
            "corpus": corpus,
            "source": source_path,
            "source_hash": source_hash,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "page_start": raw.page_start,
            "page_end": raw.page_end,
            "section": raw.section_path,
            "summary": summary,
            "token_count": len(raw.content) // 4,
            "doc_type": doc_type,
        }
        if doc_type in {"datasheet", "app_note"}:
            mpn, manufacturer = extract_mpn_and_manufacturer(raw.content[:3000])
            metadata["mpn"] = mpn
            metadata["manufacturer"] = manufacturer
        metadata.update(raw.extras)
        enriched.append({"content": raw.content, "metadata": metadata})
    return enriched
