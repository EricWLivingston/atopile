"""Section-aware datasheet chunker — the single biggest retrieval-quality lever.

Splits parsed markdown on ``##`` section headings so each datasheet section (Electrical
Characteristics, Absolute Maximum Ratings, ...) becomes one chunk with its tables/
conditions intact. Oversized sections are sub-split on ``###``. Page range is recovered
from the ``<!-- page N -->`` markers the parser injects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import (
    DATASHEET_MAX_TOKENS,
    TEXTBOOK_OVERLAP_TOKENS,
    TEXTBOOK_TARGET_TOKENS,
    DocType,
)

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


def _window_split(md: str, target: int, overlap: int) -> list[str]:
    """Split ``md`` into ~``target``-token windows at paragraph boundaries.

    Each window starts with the trailing ~``overlap`` tokens of the previous one —
    at minimum its last paragraph, even when that alone exceeds the overlap budget
    (typical prose paragraphs do), so a derivation or worked example spanning a
    boundary stays retrievable from either side. Only a pathologically large last
    paragraph (> half the window target) is not carried over.
    """
    paragraphs = re.split(r"\n{2,}", md)
    windows: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for para in paragraphs:
        para_tokens = approx_tokens(para)
        if current and current_tokens + para_tokens > target:
            windows.append("\n\n".join(current))
            kept: list[str] = []
            kept_tokens = 0
            for prev in reversed(current):
                prev_tokens = approx_tokens(prev)
                if kept and kept_tokens + prev_tokens > overlap:
                    break
                if not kept and prev_tokens > target // 2:
                    break
                kept.insert(0, prev)
                kept_tokens += prev_tokens
            current, current_tokens = kept, kept_tokens
        current.append(para)
        current_tokens += para_tokens
    if current:
        windows.append("\n\n".join(current))
    return windows


def chunk_textbook(parsed_md: str) -> list[RawChunk]:
    """Heading-aware textbook chunker: ## chapters, ### sections, windowed bodies.

    Sections at or under ``TEXTBOOK_TARGET_TOKENS`` stay whole; oversized ones are
    window-split at paragraph boundaries with ``TEXTBOOK_OVERLAP_TOKENS`` of overlap.
    The chapter heading lands in ``extras`` so enrich can store it as metadata.
    """
    chunks: list[RawChunk] = []
    for chapter, chapter_md in _split_at_headings(parsed_md, H2_SPLIT):
        # Drop front-matter noise before the first chapter (cover/TOC fragments).
        if not chapter and approx_tokens(chapter_md) < 50:
            continue
        if approx_tokens(chapter_md) <= TEXTBOOK_TARGET_TOKENS:
            sections = [("", chapter_md)]
        else:
            sections = _split_at_headings(chapter_md, H3_SPLIT)
        for sub_heading, sub_md in sections:
            path = " > ".join(p for p in (chapter, sub_heading) if p) or "(preamble)"
            if approx_tokens(sub_md) <= TEXTBOOK_TARGET_TOKENS:
                windows = [sub_md]
            else:
                windows = _window_split(
                    sub_md, TEXTBOOK_TARGET_TOKENS, TEXTBOOK_OVERLAP_TOKENS
                )
            for i, window in enumerate(windows):
                ps, pe = _find_page_range(window)
                chunks.append(
                    RawChunk(
                        content=window,
                        section_path=(
                            path
                            if len(windows) == 1
                            else f"{path} [{i + 1}/{len(windows)}]"
                        ),
                        page_start=ps,
                        page_end=pe,
                        extras={"chapter": chapter} if chapter else {},
                    )
                )
    return chunks


# Datasheets and app_notes share the section-aware chunker. Remaining corpora
# (standards: clause-aware; internal: heading-aware) are follow-ups.
_CHUNKERS = {
    "datasheet": chunk_datasheet,
    "app_note": chunk_datasheet,
    "textbook": chunk_textbook,
}


def chunk_dispatch(parsed_md: str, doc_type: DocType) -> list[RawChunk]:
    if doc_type not in _CHUNKERS:
        raise NotImplementedError(
            f"No chunker for doc_type={doc_type!r} yet "
            "(have: datasheets/app_notes/textbooks)."
        )
    return _CHUNKERS[doc_type](parsed_md)
