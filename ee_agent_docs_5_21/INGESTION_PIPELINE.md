# RAG Ingestion Pipeline — Implementation Plan

> **Premise:** The retriever is only as good as what got ingested. For engineering documents — datasheets, standards, app notes, textbooks — the parsing and chunking decisions made at ingest time *cap* retrieval quality forever. Spend the time here.

---

## 1. Why this is harder than generic RAG

A normal RAG pipeline can get away with "PyPDF → split every 512 tokens → embed → store." That fails for our corpus because:

1. **Datasheets have tables that are the answer.** "Electrical Characteristics" is a table where the column is the test condition and the row is the parameter. A naive chunker cuts it mid-row and produces garbage. A parser that flattens it to prose ("Vout typ 1.8V min 1.78V max 1.82V at Iout=10mA Vin=3.0V Ta=25C") preserves the meaning.
2. **Standards are clause-addressable.** Engineers cite "IPC-2221B §6.2 page 33." Retrieval must return the *clause* as the unit, with the clause number in metadata, so `get_standard_clause("IPC-2221B", "6.2")` is a deterministic lookup rather than a search.
3. **Textbooks have heavy cross-references.** A passage on op-amp stability references Figure 4.27 and Equation 4.18. Losing the figure context breaks the explanation.
4. **PDFs have OCR landmines.** Old MIL-STDs, some Asian-vendor datasheets, and scanned app notes need OCR. Mixing OCR'd and native-text PDFs in one pipeline drops quality.
5. **Versioning matters.** IPC-2221A vs IPC-2221B. SHT41 datasheet rev 5 vs rev 6. Wrong-revision answers are worse than no answer.

The ingestion pipeline addresses all five.

---

## 2. Pipeline overview

```
                       ┌─────────────────────────────┐
                       │  Source documents           │
                       │  • datasheets (vendor PDFs) │
                       │  • app notes (PDFs)         │
                       │  • standards (paid PDFs)    │
                       │  • textbooks (PDFs/EPUBs)   │
                       │  • internal docs (md, docx) │
                       └──────────────┬──────────────┘
                                      ▼
              ┌─────────────────────────────────────────────┐
              │  Step 1: Identify document type             │
              │  filename heuristics + first-page detector  │
              └──────────────┬──────────────────────────────┘
                             ▼
              ┌─────────────────────────────────────────────┐
              │  Step 2: Parse                              │
              │  type-specific parser (LlamaParse / Docling │
              │  / native md handler)                       │
              └──────────────┬──────────────────────────────┘
                             ▼
              ┌─────────────────────────────────────────────┐
              │  Step 3: Type-specific chunk                │
              │  datasheets: section-aware                  │
              │  standards: clause-aware                    │
              │  textbooks: heading-aware                   │
              └──────────────┬──────────────────────────────┘
                             ▼
              ┌─────────────────────────────────────────────┐
              │  Step 4: Enrich metadata                    │
              │  extract MPN, clause #, page #, title       │
              │  generate one-line summary (Haiku)          │
              └──────────────┬──────────────────────────────┘
                             ▼
              ┌─────────────────────────────────────────────┐
              │  Step 5: Embed + index                      │
              │  voyage-3 embeddings → Qdrant collection    │
              │  rebuild BM25 index for the collection      │
              └──────────────┬──────────────────────────────┘
                             ▼
              ┌─────────────────────────────────────────────┐
              │  Step 6: Eval check (CI)                    │
              │  run retrieval on the corpus' eval set      │
              │  block deploy if recall@5 drops             │
              └─────────────────────────────────────────────┘
```

Each step is implemented as a separate module so you can swap any of them (parser, chunker, embedder) without rewriting the rest.

---

## 3. Project structure

```
ee_agent/src/ee_agent/rag/
├── __init__.py
├── config.py                  # CORPUS_CONFIG, embedding model, store config
├── classify.py                # Step 1: identify doc type
├── parse/
│   ├── __init__.py            # dispatch by type
│   ├── datasheet_parser.py    # LlamaParse with electronics prompt
│   ├── standard_parser.py     # LlamaParse w/ clause detection
│   ├── textbook_parser.py     # Docling for textbooks
│   └── markdown_parser.py     # internal docs (md, docx)
├── chunk/
│   ├── __init__.py            # dispatch by type
│   ├── datasheet_chunker.py   # section-aware
│   ├── standard_chunker.py    # clause-aware
│   ├── textbook_chunker.py    # heading-aware w/ overlap
│   └── markdown_chunker.py    # by H2/H3
├── enrich.py                  # Step 4: metadata extraction, summarization
├── embed.py                   # Step 5: voyage-3 embeddings
├── store.py                   # Qdrant + BM25 wrappers
├── retriever.py               # the query-time pipeline (used by rag_search tool)
├── eval/
│   ├── datasets/              # per-corpus eval sets (jsonl)
│   ├── runner.py              # recall@K, MRR
│   └── ci_check.py            # gate for ingestion runs
└── ingest.py                  # CLI: `python -m ee_agent.rag.ingest path/to/doc.pdf`
```

---

## 4. Step 1 — Identify document type

Cheap heuristics first; LLM classifier only if needed.

```python
# rag/classify.py
import re
from pathlib import Path

DocType = Literal["datasheet", "app_note", "standard", "textbook", "internal", "unknown"]

DATASHEET_PATTERNS = [
    r"\b(datasheet|data sheet)\b",
    r"\bDS\d{4,}\b",
    r"\bRev\.? [A-Z]\d?\b.*\bdatasheet\b",
]

STANDARD_PATTERNS = [
    r"\bIPC-\d{4}",
    r"\bMIL-STD-\d+",
    r"\bJEDEC\b.*\bJESD",
    r"\bIEC \d{4,}",
    r"\bISO/IEC \d{4,}",
]

def classify(pdf_path: Path) -> DocType:
    # 1. Filename
    name = pdf_path.stem.lower()
    if any(re.search(p, name, re.I) for p in STANDARD_PATTERNS):
        return "standard"
    if any(re.search(p, name, re.I) for p in DATASHEET_PATTERNS):
        return "datasheet"

    # 2. First-page text scan (cheap pdfminer extract)
    first_page = extract_first_page_text(pdf_path)
    if any(re.search(p, first_page, re.I) for p in STANDARD_PATTERNS):
        return "standard"
    if "Electrical Characteristics" in first_page and re.search(r"\bV(in|out|cc|dd)\b", first_page):
        return "datasheet"
    if re.search(r"application note|app note|AN-?\d+", first_page, re.I):
        return "app_note"

    # 3. Path-based fallback (e.g. /textbooks/aoe_ch4.pdf → textbook)
    parts = [p.lower() for p in pdf_path.parts]
    if "textbooks" in parts: return "textbook"
    if "internal" in parts: return "internal"

    # 4. Last resort: ask Haiku
    return classify_with_llm(first_page)
```

The LLM fallback (`classify_with_llm`) runs Haiku with a one-shot prompt; this should be <2% of documents in practice.

---

## 5. Step 2 — Parsing

Different parsers for different document types. The recommendation is **LlamaParse** for datasheets and standards (best table handling in published comparisons), **Docling** as a self-hosted alternative or for textbooks, and a native handler for markdown/docx.

### 5.1 Datasheet parser

LlamaParse with a parsing instruction tuned for electronics docs:

```python
# rag/parse/datasheet_parser.py
from llama_parse import LlamaParse

DATASHEET_INSTRUCTION = """
You are parsing an electronics datasheet. Output well-structured markdown.

Required behavior:
- Detect and preserve section headings (e.g. "Electrical Characteristics",
  "Absolute Maximum Ratings", "Typical Application Circuit", "Pin Description",
  "Ordering Information"). Render as ## headings.
- Tables MUST be preserved as markdown tables with all column headers intact.
  Do NOT flatten multi-row headers; merge them with " / " separator.
- For "Electrical Characteristics" tables, preserve test conditions
  (Vin, Iout, Ta) — they belong with the row, not just the table header.
- Footnotes referenced from table cells must be inlined as italicized notes
  after the table.
- Preserve the manufacturer part number (MPN) prominently — usually on the
  first page in a heading or banner.
- Skip image content unless it's a schematic or pinout diagram, in which
  case describe it as: "[Diagram: <one-sentence description>]".
"""

def parse_datasheet(pdf_path: str) -> list[ParsedSection]:
    parser = LlamaParse(
        api_key=os.environ["LLAMA_CLOUD_API_KEY"],
        result_type="markdown",
        parsing_instruction=DATASHEET_INSTRUCTION,
        invalidate_cache=False,   # cache is gold; same PDF parsed twice is free
    )
    documents = parser.load_data(pdf_path)
    md = "\n".join(d.text for d in documents)
    return md  # downstream chunker splits it
```

A note on cost: LlamaParse charges per page. A 60-page MCU datasheet runs ~6¢. If you're ingesting 500 datasheets it's $30 total — cheap. For an internal-only setup or to avoid the API entirely, `Docling` (IBM open-source) is the strongest self-hosted option for tables; quality is close but not identical.

### 5.2 Standard parser

Standards are clause-addressable, so the parser's job is to surface the clause numbers cleanly.

```python
# rag/parse/standard_parser.py
STANDARD_INSTRUCTION = """
You are parsing an industry standard document (IPC, MIL-STD, IEC, JEDEC).

Required behavior:
- Preserve clause numbering exactly: "6.2", "6.2.1", "6.2.1.1". Render as
  markdown headings where the heading level matches the clause depth:
  "## 6.2 Conductor Width and Thickness", "### 6.2.1 ...".
- Preserve table numbers and figure numbers as they appear ("Table 6-2",
  "Figure 5-3"). Cross-references in body text must remain intact.
- For tables, preserve the original table header structure. Don't collapse
  units (mil, mm, in) — keep both columns if both are present.
- Footnotes and notes (often denoted "Note:") must remain associated with
  the clause they belong to.
- Skip the document's revision history table — it's not useful for retrieval.
"""
```

### 5.3 Textbook parser

Textbooks (Art of Electronics, Razavi, Sedra/Smith) usually come as multi-hundred-page PDFs. LlamaParse handles them but Docling is often cheaper at scale. Either way, parse chapter-by-chapter, not the whole book at once.

### 5.4 Markdown / docx parser

For your internal docs:

```python
# rag/parse/markdown_parser.py
def parse_markdown(path: Path) -> str:
    if path.suffix == ".md":
        return path.read_text()
    elif path.suffix == ".docx":
        import mammoth
        with open(path, "rb") as f:
            return mammoth.convert_to_markdown(f).value
```

---

## 6. Step 3 — Chunking

This is the highest-leverage decision in the whole pipeline.

### 6.1 Datasheet chunker

The atomic unit is a **section**. Each section becomes one chunk, regardless of token count, *unless* it exceeds a hard cap (~3000 tokens) — then split on sub-sections.

```python
# rag/chunk/datasheet_chunker.py
import re
from dataclasses import dataclass

# Sections we want to preserve as units, in order of preference
PRIORITY_SECTIONS = [
    "Electrical Characteristics",
    "Absolute Maximum Ratings",
    "Recommended Operating Conditions",
    "Typical Application Circuit",
    "Pin Description",
    "Pin Configuration",
    "Functional Block Diagram",
    "Ordering Information",
    "Package Information",
    "Mechanical Drawing",
    "Thermal Information",
]

@dataclass
class DatasheetChunk:
    section: str
    content: str
    page_start: int
    page_end: int
    mpn: str | None
    table_count: int        # for downstream weighting

def chunk_datasheet(parsed_md: str, source_path: str) -> list[DatasheetChunk]:
    # Split on H2 (## section headers)
    sections = re.split(r"^## ", parsed_md, flags=re.M)
    mpn = extract_mpn(parsed_md)

    chunks = []
    for sec in sections:
        if not sec.strip():
            continue
        first_line, _, rest = sec.partition("\n")
        section_name = first_line.strip()
        content = "## " + sec

        # Hard cap: split overly long sections at H3 boundaries
        if token_count(content) > 3000:
            for sub in split_at_h3(content):
                chunks.append(DatasheetChunk(
                    section=f"{section_name} > {sub['heading']}",
                    content=sub["content"],
                    page_start=sub["page_start"],
                    page_end=sub["page_end"],
                    mpn=mpn,
                    table_count=count_md_tables(sub["content"]),
                ))
        else:
            chunks.append(DatasheetChunk(
                section=section_name,
                content=content,
                page_start=find_page(content),
                page_end=find_page(content, last=True),
                mpn=mpn,
                table_count=count_md_tables(content),
            ))

    return chunks
```

**Key design decisions:**
- Section is the unit. Don't break a table across chunks.
- Sub-section split only when forced by length cap.
- `page_start` / `page_end` come from page anchors LlamaParse embeds in the markdown.
- `mpn` propagates to every chunk so a query like "TLV713P PSRR" hits this part's chunks.
- `table_count` lets the retriever boost table-heavy chunks for parametric queries.

### 6.2 Standard chunker

Atomic unit is a **clause**. Clause IDs become first-class metadata.

```python
# rag/chunk/standard_chunker.py
CLAUSE_PATTERN = re.compile(r"^(#{2,5})\s+([\d.]+)\s+(.+?)$", re.M)

@dataclass
class StandardChunk:
    standard: str       # "IPC-2221B"
    revision: str       # "B"
    clause: str         # "6.2.1"
    title: str          # "Conductor Width"
    content: str
    page_start: int

def chunk_standard(parsed_md: str, standard_name: str, revision: str) -> list[StandardChunk]:
    # Find every clause heading. Content runs until next equal-or-shallower heading.
    matches = list(CLAUSE_PATTERN.finditer(parsed_md))
    chunks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(parsed_md)
        depth_hashes, clause_id, title = m.group(1), m.group(2), m.group(3)
        content = parsed_md[start:end]
        chunks.append(StandardChunk(
            standard=standard_name,
            revision=revision,
            clause=clause_id,
            title=title.strip(),
            content=content,
            page_start=find_page(content),
        ))
    return chunks
```

This makes the `get_standard_clause("IPC-2221B", "6.2.1")` deterministic lookup trivial — just a metadata filter on the collection.

### 6.3 Textbook chunker

Heading-aware with overlap. Textbooks reward overlap (concepts span sections); datasheets don't (sections are self-contained).

```python
# rag/chunk/textbook_chunker.py
def chunk_textbook(parsed_md: str, book: str, chapter: str) -> list[TextbookChunk]:
    # Split on H2 (sections) and H3 (sub-sections).
    # Target chunk size: ~600 tokens with ~100-token overlap.
    sections = split_on_headings(parsed_md, levels=[2, 3])
    chunks = []
    for sec in sections:
        sub_chunks = sliding_window_chunks(
            sec.content,
            target_tokens=600,
            overlap_tokens=100,
        )
        for sc in sub_chunks:
            chunks.append(TextbookChunk(
                book=book,
                chapter=chapter,
                section=sec.title,
                content=sc,
                page_start=sec.page_start,
            ))
    return chunks
```

### 6.4 Markdown chunker

```python
def chunk_markdown(content: str, doc_id: str) -> list[InternalChunk]:
    # Split on H2; if a section is >1500 tokens, fall through to H3.
    # Internal docs are usually well-structured, so simple splits work.
    ...
```

### 6.5 `.ato` chunker (for `atopile_examples` corpus)

Atopile source files use one chunk per `module` block, because modules are the atomic unit of reuse. Imports are duplicated into each chunk so the chunk is standalone.

```python
import re
from pathlib import Path

MODULE_RE = re.compile(
    r"^module\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:?(?P<body>(?:\n(?:    .*|\n))+)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(r"^(?:import\s+\S+|from\s+\".+?\"\s+import\s+\S+)\s*$", re.MULTILINE)
PRAGMA_RE = re.compile(r"^#pragma\s+.*$", re.MULTILINE)
STDLIB_TYPES = {
    "ElectricPower", "ElectricLogic", "Electrical",
    "I2C", "SPI", "CAN", "USB2_0", "USB3_0", "UART",
    "Resistor", "Capacitor", "Inductor", "Diode", "LED", "Fuse",
    # ... full 102-name list pre-cached
}

def chunk_ato(path: Path, project: str) -> list[InternalChunk]:
    """One chunk per `module` block. Imports + pragmas attached to each."""
    content = path.read_text()
    imports = "\n".join(IMPORT_RE.findall(content))
    pragmas = "\n".join(PRAGMA_RE.findall(content))

    chunks = []
    for match in MODULE_RE.finditer(content):
        name = match.group("name")
        body = match.group(0)  # includes module declaration + body
        # Re-attach imports for self-containment
        chunk_text = f"{pragmas}\n\n{imports}\n\n{body}".strip()
        stdlib_used = sorted({
            tok for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", body)
            if tok in STDLIB_TYPES
        })
        chunks.append(InternalChunk(
            text=chunk_text,
            doc_id=str(path),
            type="ato_module",
            metadata={
                "project": project,
                "module_name": name,
                "path": str(path),
                "imports": IMPORT_RE.findall(content),
                "stdlib_types_used": stdlib_used,
            },
        ))
    return chunks


def ingest_atopile_examples(examples_root: Path):
    """Walk every .ato file under examples_root and chunk per-module."""
    for ato_path in examples_root.rglob("*.ato"):
        # Skip .ato files inside .pyc, build/, .venv/, etc.
        if any(p.startswith(".") or p == "build" for p in ato_path.parts):
            continue
        project = _derive_project(ato_path, examples_root)
        chunks = chunk_ato(ato_path, project)
        # ... embed + insert into atopile_examples Qdrant collection ...


def _derive_project(ato_path: Path, examples_root: Path) -> str:
    """Project name from the directory immediately under examples_root."""
    rel = ato_path.relative_to(examples_root)
    return rel.parts[0] if rel.parts else "unknown"
```

For long modules (>8k tokens — rare but possible for big MCU wrappers), fall back to splitting by interface group inside the module. Keep the module name in metadata so the chunks are still retrievable as a unit.

The chunker for `atopile_docs` is just the existing markdown chunker (`chunk_markdown`) applied to `.claude/skills/*/SKILL.md` and `docs.atopile.io` pages, with `corpus="atopile_docs"` and `version` metadata from the atopile package version.

---

## 7. Step 4 — Metadata enrichment

Every chunk gets the same baseline metadata plus type-specific extras.

### 7.1 Baseline metadata

```python
@dataclass
class ChunkMetadata:
    chunk_id: str              # stable hash of (source_path, section/clause, content[:200])
    corpus: str                # "datasheets" | "app_notes" | "standards" | "textbooks" | "internal"
    source: str                # absolute path or URL
    source_hash: str           # SHA-256 of the source file (for re-ingest detection)
    ingested_at: str           # ISO 8601
    page_start: int | None
    page_end: int | None
    summary: str               # one-line, LLM-generated
    token_count: int
```

`chunk_id` must be **stable across re-ingests of the same source**. The hash incorporates source path + section anchor + content prefix, so a tiny edit to the source still produces the same chunk ID for unchanged sections. This matters because the agent's outputs cite chunk IDs — those citations should still resolve after a re-ingest.

### 7.2 Type-specific metadata

| Corpus | Extra fields |
|---|---|
| datasheets | `mpn`, `manufacturer`, `package`, `section`, `table_count` |
| app_notes | `manufacturer`, `doc_id`, `title`, `topic_tags`, `referenced_mpns[]` |
| standards | `standard`, `revision`, `clause`, `clause_depth`, `title` |
| textbooks | `book`, `edition`, `chapter`, `section` |
| internal | `doc_id`, `owner`, `last_review`, `tags` |

### 7.3 The summary field

A one-line summary helps with two things: (1) query rewriting (the rewriter can match against summaries), (2) showing the user *why* a chunk was retrieved. Generate with Haiku:

```python
# rag/enrich.py
from langchain_anthropic import ChatAnthropic

summarizer = ChatAnthropic(model="claude-haiku-4-5", max_tokens=80)

SUMMARY_PROMPT = """Summarize this engineering document chunk in ONE sentence (≤25 words).
Focus on what specific fact, parameter, rule, or design pattern it covers.

Chunk:
{content}

One-sentence summary:"""

async def summarize_chunk(content: str) -> str:
    # Truncate content to 2000 chars for the prompt
    response = await summarizer.ainvoke(SUMMARY_PROMPT.format(content=content[:2000]))
    return response.content.strip()
```

Batch these — running 500 chunks one at a time wastes minutes. Use `summarizer.abatch([...])` with concurrency 20.

### 7.4 MPN and manufacturer extraction

For datasheets, surfacing MPN and manufacturer in metadata is non-negotiable. A combination of regex (for the MPN — they follow predictable patterns per manufacturer) + a small Haiku call on the first chunk works well:

```python
def extract_mpn_and_manufacturer(first_page_md: str) -> tuple[str, str]:
    # Regex first for known patterns
    for pattern, manufacturer in [
        (r"\b(STM32[A-Z]\d+[A-Z0-9]+)\b", "STMicroelectronics"),
        (r"\b(nRF\d{5}-[A-Z]+-?R?\d?)\b", "Nordic Semi"),
        (r"\b(TLV\d{3,4}[A-Z0-9-]+)\b", "Texas Instruments"),
        (r"\b(LM[0-9]{2,4}[A-Z0-9-]+)\b", "Texas Instruments"),
        # ... extend with each new manufacturer encountered
    ]:
        m = re.search(pattern, first_page_md)
        if m:
            return m.group(1), manufacturer
    # Fallback: ask Haiku
    return extract_with_llm(first_page_md)
```

---

## 8. Step 5 — Embed and index

### 8.1 Embedding model choice

Three credible options for technical text in 2026:

| Model | Strengths | Caveats |
|---|---|---|
| `voyage-3` (Voyage AI) | Best published performance on technical retrieval, 16k context | Paid API |
| `text-embedding-3-large` (OpenAI) | Widely available, 8k context, well-supported | Slightly worse on parametric queries vs Voyage |
| `bge-large-en-v1.5` (BAAI) | Free, self-hostable | Older; consider `bge-m3` for newer |

Default to `voyage-3`. Wrap behind an interface so swapping is trivial:

```python
# rag/embed.py
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

class VoyageEmbedder:
    def __init__(self, model="voyage-3"):
        self.client = voyageai.AsyncClient()
        self.model = model
    async def embed(self, texts: list[str]) -> list[list[float]]:
        # voyage supports batches of 128
        out = []
        for batch in chunked(texts, 128):
            r = await self.client.embed(batch, model=self.model, input_type="document")
            out.extend(r.embeddings)
        return out
```

Note `input_type="document"` for ingestion vs `"query"` for retrieval — Voyage's models are asymmetric.

### 8.2 Vector store

`Qdrant` for prod (good filtering, fast metadata queries, scales), `Chroma` for dev (zero-setup, file-backed). Same interface:

```python
# rag/store.py
from qdrant_client import QdrantClient, models

class QdrantStore:
    def __init__(self, url, collection):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self._ensure_collection()

    def _ensure_collection(self):
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=1024,   # voyage-3 dim
                    distance=models.Distance.COSINE,
                ),
            )
            # Payload indexes for fast filtering
            for field in ["mpn", "manufacturer", "standard", "clause", "corpus"]:
                self.client.create_payload_index(
                    self.collection, field, models.PayloadSchemaType.KEYWORD,
                )

    def upsert(self, chunks: list[dict], embeddings: list[list[float]]):
        points = [
            models.PointStruct(
                id=c["chunk_id"],
                vector=v,
                payload={**c["metadata"], "content": c["content"]},
            )
            for c, v in zip(chunks, embeddings)
        ]
        self.client.upsert(self.collection, points=points)
```

One Qdrant collection per corpus (`datasheets`, `app_notes`, `standards`, etc.). Cross-corpus retrieval = query each, ensemble the results.

### 8.3 BM25 index

Sparse retrieval is essential for datasheet parametric queries ("PSRR 60 dB", part numbers, specific units). LangChain's `BM25Retriever` works but lives in-memory only; for multi-process deployments use Qdrant's built-in sparse vectors or an OpenSearch sidecar.

For dev / single-process:

```python
# rag/store.py
from langchain_community.retrievers import BM25Retriever

def build_bm25_for_corpus(corpus: str, chunks: list[dict]) -> BM25Retriever:
    docs = [Document(page_content=c["content"], metadata=c["metadata"]) for c in chunks]
    return BM25Retriever.from_documents(docs, k=20)
```

Persist by pickling the retriever to disk and reloading on startup. Rebuild on every ingest run for the affected corpus.

For prod, use Qdrant's hybrid (dense + sparse via SPLADE or BM42):

```python
self.client.create_collection(
    collection_name=self.collection,
    vectors_config={"dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)},
    sparse_vectors_config={"sparse": models.SparseVectorParams()},
)
```

---

## 9. Step 6 — Evaluation gate

Every ingestion run ends with a recall check against a per-corpus eval set. If recall@5 drops, the run fails — you don't want a re-parse to silently degrade quality.

### 9.1 Eval set format

```jsonl
{"query": "What's the typical PSRR of the TLV713P at 1kHz?", "must_contain_chunk_id": "tlv713p_electrical_chars_p3"}
{"query": "IPC-2221 minimum trace width for external 1oz 1A 10C rise", "must_contain_chunk_id": "ipc_2221b_6_2_p33"}
{"query": "Why does an op-amp need compensation capacitance?", "must_contain_chunk_id": "aoe_ch4_stability_p218"}
```

Build these incrementally as you go — start with 10 per corpus, grow to 50+. The work pays off forever.

### 9.2 Eval runner

```python
# rag/eval/runner.py
async def run_eval(corpus: str, eval_set_path: str, retriever, k=5) -> dict:
    queries = load_jsonl(eval_set_path)
    hits = 0
    for q in queries:
        results = await retriever.aretrieve(q["query"], top_k=k)
        if q["must_contain_chunk_id"] in [r.metadata["chunk_id"] for r in results]:
            hits += 1
    return {
        "corpus": corpus,
        "recall_at_5": hits / len(queries),
        "total_queries": len(queries),
    }
```

### 9.3 CI gate

```python
# rag/eval/ci_check.py
BASELINES = {
    "datasheets": 0.80,
    "standards": 0.90,   # higher bar — these are clause-addressable
    "textbooks": 0.65,   # lower bar — open-domain
    "app_notes": 0.70,
    "internal": 0.85,
}

def gate_ingestion(eval_results: list[dict]) -> bool:
    for r in eval_results:
        if r["recall_at_5"] < BASELINES[r["corpus"]]:
            print(f"FAIL: {r['corpus']} recall@5={r['recall_at_5']:.2f} "
                  f"< baseline {BASELINES[r['corpus']]}")
            return False
    return True
```

Wire this into a GitHub Actions step or pre-deploy hook.

---

## 10. The full ingest CLI

```python
# rag/ingest.py
import argparse
from pathlib import Path

async def ingest(path: Path, force_reingest: bool = False):
    # 1. Classify
    doc_type = classify(path)
    corpus = {
        "datasheet": "datasheets",
        "app_note": "app_notes",
        "standard": "standards",
        "textbook": "textbooks",
        "internal": "internal",
    }[doc_type]

    # 2. Skip if already ingested and source unchanged
    source_hash = sha256(path)
    if not force_reingest and store.has_source(source_hash):
        print(f"Skip: {path} unchanged since last ingest")
        return

    # 3. Parse
    parser = PARSERS[doc_type]
    parsed_md = await parser.parse(path)

    # 4. Chunk
    chunker = CHUNKERS[doc_type]
    chunks = chunker.chunk(parsed_md, source_path=str(path))

    # 5. Enrich
    enriched = await enrich_chunks(chunks, corpus=corpus, source_hash=source_hash)

    # 6. Embed
    embeddings = await embedder.embed([c["content"] for c in enriched])

    # 7. Upsert
    store.upsert(collection=corpus, chunks=enriched, embeddings=embeddings)
    rebuild_bm25(corpus)

    # 8. Eval (lightweight check — just verify newly ingested chunks are retrievable)
    smoke_test(corpus, enriched)

    print(f"Ingested {len(enriched)} chunks from {path} into '{corpus}'")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(ingest(args.path, force_reingest=args.force))
```

Usage:

```bash
python -m ee_agent.rag.ingest data/datasheets/TLV713P.pdf
python -m ee_agent.rag.ingest data/standards/IPC-2221B.pdf
python -m ee_agent.rag.ingest data/internal/design_guide_v3.md
```

For bulk ingestion:

```bash
find data/datasheets -name '*.pdf' | xargs -I {} -P 4 python -m ee_agent.rag.ingest {}
```

`-P 4` runs 4 workers in parallel. The LlamaParse API handles concurrent requests fine, but watch your rate limit on the embedder.

---

## 11. Operational concerns

### 11.1 Re-ingest detection
`source_hash` (SHA-256 of the file) plus `chunk_id` (stable hash of section + content prefix) means:
- Same file ingested twice → skipped entirely
- File updated (e.g. datasheet rev D → rev E) → re-parses, but unchanged chunks keep their IDs, so existing citations still resolve
- Only truly-changed chunks get re-embedded and re-indexed

### 11.2 Incremental embedding cost
Voyage charges per token. A 60-page datasheet ≈ 80k tokens ≈ $0.014. 500 datasheets ≈ $7. Standards (denser) and textbooks (huge) cost more — IPC-2221B alone might be $1 of embeddings. Budget $50-100 for an initial corpus build.

### 11.3 LlamaParse caching
LlamaParse caches by file hash on their side. Re-running ingestion on the same file is essentially free for parsing.

### 11.4 Versioning standards
When IPC-2221C eventually replaces IPC-2221B:
- Ingest the new doc with `standard="IPC-2221C"` (the chunker pulls this from the parsed text)
- Don't delete IPC-2221B chunks — older designs may still cite them
- Add a `is_latest` boolean to standards metadata; default retrieval filters to `is_latest=true`

### 11.5 Internal-doc PII / secrets
Internal docs may contain unreleased part numbers or supplier pricing. Tag them with an `access_level` field at ingest; the retriever respects this when called with a user context that has a lower access level. Out of scope for v1 but worth knowing the seam exists.

### 11.6 Re-embed sweeps
Voyage will release new embedding models every 6-12 months. Plan for a periodic full re-embed. Keep the parsed markdown on disk so you don't need to re-parse — just re-embed and re-index.

---

## 12. Eval set seeding

Before you have any RAG quality data, you have to seed the eval sets. The fastest way:

1. **Use the agent itself.** Run 10 realistic design questions through a baseline agent (no RAG). Note every claim that *should* have been grounded in a specific document. Make each one an eval entry.
2. **Sample from real engineer questions.** Slack archives, JIRA issues, "ask the EE channel" threads. These are the actual query distribution.
3. **Aim for 30 per corpus** before the first deploy. Grow to 100+ over the first quarter.

A good eval entry includes:
- The natural-language query as it would actually be asked
- The chunk ID that *must* be in top-K
- (Optional) the chunk IDs that *must not* be in top-K — useful for catching wrong-version retrievals

---

## 13. Build order

1. **Get the classifier + a single parser path working.** Pick datasheets first (highest volume, biggest quality lever).
2. **Implement the datasheet chunker.** Validate by eye on 5 datasheets — does the section split look right?
3. **Plumb embed + Qdrant.** A single corpus end-to-end is more useful than five half-built ones.
4. **Add the BM25 index and hybrid retrieval.** Now you have queryable data.
5. **Build the 30-question datasheet eval set.** Measure baseline recall@5.
6. **Iterate on parsing instructions and chunking.** This is the meat of the work. Each tweak — say, preserving footnote associations — should ratchet recall@5 up.
7. **Add the next corpus** (standards next, since they're the highest leverage for the verification workflow).
8. **Wire ingestion into CI.** Pre-commit hook or scheduled job that re-runs the eval suite on a fresh ingestion against `main`.
9. **Add textbooks last.** They're the lowest leverage per dollar and the chunking is fussier.
