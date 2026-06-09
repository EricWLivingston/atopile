"""Section-aware datasheet chunker — the single biggest retrieval-quality lever.

Splits parsed markdown on ``##`` section headings so each datasheet section (Electrical
Characteristics, Absolute Maximum Ratings, ...) becomes one chunk with its tables/
conditions intact. Oversized sections are sub-split on ``###``. Page range is recovered
from the ``<!-- page N -->`` markers the parser injects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import DATASHEET_MAX_TOKENS, DocType

H2_SPLIT = re.compile(r"^##\s+(.+?)$", re.M)
H3_SPLIT = re.compile(r"^###\s+(.+?)$", re.M)
PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s*-->")


@dataclass
class RawChunk:
    content: str
    section_path: str
    page_start: int | None = None
    page_end: int | None = None
    extras: dict = field(default_factory=dict)


def approx_tokens(text: str) -> int:
    return len(text) // 4


def _find_page_range(content: str) -> tuple[int | None, int | None]:
    pages = [int(m.group(1)) for m in PAGE_MARKER.finditer(content)]
    if not pages:
        return None, None
    return min(pages), max(pages)


def _split_at_headings(md: str, splitter: re.Pattern) -> list[tuple[str, str]]:
    """Return ``[(heading, content_starting_at_heading), ...]``."""
    matches = list(splitter.finditer(md))
    if not matches:
        return [("", md)]
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out.append((m.group(1).strip(), md[start:end]))
    return out


def chunk_datasheet(parsed_md: str) -> list[RawChunk]:
    chunks: list[RawChunk] = []
    for section_heading, section_md in _split_at_headings(parsed_md, H2_SPLIT):
        # Drop the tiny pre-first-section preamble (banner/logo noise).
        if not section_heading and approx_tokens(section_md) < 50:
            continue

        if approx_tokens(section_md) <= DATASHEET_MAX_TOKENS:
            ps, pe = _find_page_range(section_md)
            chunks.append(
                RawChunk(
                    content=section_md,
                    section_path=section_heading or "(preamble)",
                    page_start=ps,
                    page_end=pe,
                )
            )
        else:
            # Too big — sub-split on H3 so a giant section doesn't dominate retrieval.
            for sub_heading, sub_md in _split_at_headings(section_md, H3_SPLIT):
                ps, pe = _find_page_range(sub_md)
                path = (
                    f"{section_heading} > {sub_heading}"
                    if sub_heading
                    else section_heading
                )
                chunks.append(
                    RawChunk(
                        content=sub_md,
                        section_path=path,
                        page_start=ps,
                        page_end=pe,
                    )
                )
    return chunks


# Datasheets and app_notes share the section-aware chunker for v1. Other corpora
# (standards: clause-aware; textbooks: heading-aware) are follow-ups.
_CHUNKERS = {
    "datasheet": chunk_datasheet,
    "app_note": chunk_datasheet,
}


def chunk_dispatch(parsed_md: str, doc_type: DocType) -> list[RawChunk]:
    if doc_type not in _CHUNKERS:
        raise NotImplementedError(
            f"No chunker for doc_type={doc_type!r} yet (v1: datasheets/app_notes only)."
        )
    return _CHUNKERS[doc_type](parsed_md)
