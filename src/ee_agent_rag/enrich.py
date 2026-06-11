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
# Order matters: first match wins, so longer/more specific prefixes go first
# (SPX before SP, LMV before LM, MC100EP before generic MC).
MPN_PATTERNS = [
    (r"\b(STM32[A-Z]\d+[A-Z0-9]+)\b", "STMicroelectronics"),
    (r"\b(nRF\d{5}-?[A-Z]*-?R?\d?)\b", "Nordic Semiconductor"),
    (r"\b(TLV\d{3,4}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(TPS\d{3,5}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(LMV\d{3}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(LM\d{2,4}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(RM4\d[A-Z]\d{3}[A-Z0-9-]*)\b", "Texas Instruments"),
    (r"\b(LT\d{4}[A-Z]?(?:-\d+)?)\b", "Analog Devices / Linear"),
    (r"\b(MAX\d{3,5}[A-Z0-9-]*)\b", "Analog Devices / Maxim"),
    (r"\b(ATmega\d+[A-Z0-9-]*)\b", "Microchip / Atmel"),
    (r"\b(MCP\d{4,5}[A-Z0-9-]*)\b", "Microchip"),
    (r"\b(ESP32-[A-Z0-9-]+)\b", "Espressif"),
    (r"\b(NCP\d{4,5}[A-Z0-9-]*)\b", "onsemi"),
    (r"\b(MC1?0?0EP\d{2,3}[A-Z0-9-]*)\b", "onsemi"),
    (r"\b(NVT\d{4}[A-Z0-9-]*)\b", "Nexperia / NXP"),
    (r"\b(Si\d{4}[A-Z]{2}[A-Z0-9-]*)\b", "Vishay Siliconix"),
    (r"\b(SPX\d{4}[A-Z0-9-]*)\b", "MaxLinear / Exar"),
    (r"\b(SP\d{4}E[A-Z0-9-]*)\b", "MaxLinear / Exar"),
    (r"\b(CD\d{4}-[A-Z0-9]+)\b", "Bourns"),
    (r"\b(ERJ[A-Z0-9]{4,})\b", "Panasonic"),
    (r"\b(LQ[GWMHP]\d{2}[A-Z0-9]{4,})\b", "Murata"),
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
            return m.group(1).rstrip("-"), manufacturer
    return None, None


def mpn_from_filename(source_path: str) -> tuple[str | None, str | None]:
    """Doc-level MPN fallback from the source filename stem.

    Corpus convention is ``<MPN>_<description>.pdf`` (the tuning doc tells users to put
    the MPN in the filename). Try the known vendor patterns against the stem (original
    and uppercased), else fall back to the first ``_``-delimited token when it looks
    like a part number (has a digit, >= 4 chars).
    """
    from pathlib import Path

    # "_" is a regex word char, so \b never fires at it — swap for spaces first.
    stem = Path(source_path).stem.replace("_", " ")
    for candidate in (stem, stem.upper()):
        mpn, manufacturer = extract_mpn_and_manufacturer(candidate)
        if mpn:
            return mpn, manufacturer
    token = re.split(r"[_\s]", stem)[0].strip(",")
    if len(token) >= 4 and re.search(r"\d", token):
        return token, None
    return None, None


def stable_chunk_id(
    source_hash: str, section_path: str, content: str, ordinal: int = 0
) -> str:
    """Stable across re-ingests if the section + first 200 chars are unchanged.

    ``ordinal`` (position within the doc) disambiguates chunks that share a section
    heading and opening text — repeated table headers / boilerplate sections collide
    otherwise, and Chroma rejects duplicate ids in one upsert.
    """
    composite = f"{source_hash}|{section_path}|{ordinal}|{content[:200]}"
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
    for ordinal, (raw, summary) in enumerate(zip(raw_chunks, summaries)):
        metadata: dict = {
            "chunk_id": stable_chunk_id(
                source_hash, raw.section_path, raw.content, ordinal
            ),
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
            if mpn is None:
                # Chunk text doesn't name the part (common for tables/graphs):
                # fall back to the doc-level MPN derived from the filename.
                mpn, manufacturer = mpn_from_filename(source_path)
            metadata["mpn"] = mpn
            metadata["manufacturer"] = manufacturer
        metadata.update(raw.extras)
        enriched.append({"content": raw.content, "metadata": metadata})
    return enriched
