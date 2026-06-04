# 09 · RAG retriever (shared resource)

## Role
The single source of truth for cited engineering knowledge. Every other agent calls into this. Quality here disproportionately affects the whole system.

> **Option C alignment.** Under the Option C architecture (see `09_HARNESS_ANALYSIS.md`), RAG is exposed to the agent as a single tool — `rag_search` — registered into atopile's `ToolRegistry`. The retrieval pipeline below (hybrid retrieval + Cohere rerank, with corpus-aware chunking) is unchanged. What changed is the surface: there's no "RAG sub-agent" or LangChain `@tool` decorator — `rag_search` is a normal atopile tool the agent calls just like `parts_search` or `build_run`. The tool wraps the retriever code described in this doc plus `RAG_IMPLEMENTATION_PLAN.md`. The retriever package lives at `src/ee_agent_rag/` outside atopile's agent module; only the thin tool wrapper lives in `src/atopile/server/agent/_ee/tools_rag.py`. See `06_ATOPILE_INTEGRATION.md` for the registration mechanism.

## Not an agent — a service
This isn't an LLM-driven sub-agent. It's a deterministic pipeline (with one optional LLM step for query rewriting) exposed as a tool: `rag_search(query, corpus?, top_k=5) -> list[Chunk]`.

## Pipeline

```
query string
   │
   ▼
[optional] query rewrite (Haiku)    ← expand acronyms, add synonyms
   │
   ▼
corpus classifier (Haiku)            ← which collection(s) to hit?
   │
   ▼
┌───────────┬───────────┬───────────┐
│   BM25    │  Dense    │ Metadata  │   parallel retrieval
│ retrieval │ retrieval │  filter   │   (each returns top-20)
└───────────┴───────────┴───────────┘
   │
   ▼
ensemble + dedupe (RRF or weighted)  ← merge to top-30
   │
   ▼
Cohere Rerank (or cross-encoder)     ← rerank to top-K
   │
   ▼
return chunks with full citation metadata
```

## Collections

| Collection | Source documents | Chunk strategy | Metadata schema |
|---|---|---|---|
| `datasheets` | Vendor PDFs, ingested via `fetch_datasheet` | Section-aware (Electrical Char, Abs Max, App Circuit, ...) | mpn, manufacturer, section, page, package |
| `app_notes` | Manufacturer app notes, white papers | Heading-aware, ~512 tokens | manufacturer, title, doc_id, topic_tags, page |
| `standards` | IPC-2221, IPC-7351, IPC-A-610, IPC-2152, MIL-STD-461, MIL-STD-810, JEDEC JESD | Clause-aware (one chunk per numbered clause) | standard, revision, clause, page, title |
| `textbooks` | Art of Electronics (Horowitz/Hill), Razavi (Design of Analog CMOS ICs), Sedra/Smith (Microelectronic Circuits), Kularatna (Power Electronics) | Heading-aware, ~600 tokens | book, edition, chapter, section, page |
| `internal_standards` | Company design rules, lessons-learned docs, Approved Parts List | Heading-aware | doc_id, owner, last_review, tags |

## Embedding model
`voyage-3` is the recommended default for technical text. Alternative: `text-embedding-3-large` (OpenAI). Voyage tends to edge out on parametric retrieval (e.g. "find LDOs with PSRR > 60 dB at 1 kHz").

## Vector store
- Dev: `Chroma` (zero-setup, single-file persistent)
- Prod: `Qdrant` or `pgvector` (better filtering, scaling)

## Chunking — the single biggest quality lever

Naive 512-token chunks destroy datasheets. The "Electrical Characteristics" table needs to live as a single unit with its conditions; splitting it mid-row makes the chunk useless.

Pipeline:
1. Parse the PDF with `unstructured` or `LlamaParse` (LlamaParse handles tables noticeably better).
2. Detect document type (datasheet, app note, standard) from filename + first-page heuristics.
3. Apply a type-specific chunker:
   - **Datasheet**: detect section headings (uppercase, bold, larger font); chunk by section. Each chunk gets `section` metadata.
   - **Standard**: detect numbered clauses (`6.2.1`, `6.2.1.1`); chunk by lowest-numbered atomic clause.
   - **Textbook**: detect chapter / section headings; chunk by subsection.
4. For each chunk, generate metadata at ingest time, including a brief LLM-generated `summary` field for query rewriting hits.

## Hybrid retrieval

```python
from langchain.retrievers import EnsembleRetriever, BM25Retriever
from langchain_community.vectorstores import Qdrant

dense_retriever = Qdrant(...).as_retriever(search_kwargs={"k": 20})
bm25_retriever = BM25Retriever.from_documents(all_docs)
bm25_retriever.k = 20

hybrid = EnsembleRetriever(
    retrievers=[bm25_retriever, dense_retriever],
    weights=[0.4, 0.6],   # bias slightly toward dense
)
```

## Reranking
Cohere Rerank v3.5 (or a local cross-encoder like `bge-reranker-v2-m3`). The pattern:

```python
from langchain_cohere import CohereRerank
from langchain.retrievers import ContextualCompressionRetriever

compressor = CohereRerank(top_n=5)
retriever = ContextualCompressionRetriever(
    base_compressor=compressor,
    base_retriever=hybrid,
)
```

## The `rag_search` tool

```python
from langchain_core.tools import tool

CORPUS_NAMES = {"datasheets", "app_notes", "standards", "textbooks", "internal_standards"}

@tool
def rag_search(
    query: str,
    corpus: list[str] | None = None,
    top_k: int = 5,
    filter: dict | None = None,
) -> list[dict]:
    """
    Search the engineering knowledge base.
    
    Args:
        query: natural-language search query
        corpus: subset of collections to search. None = all.
        top_k: number of chunks to return after rerank
        filter: optional metadata filter, e.g. {"mpn": "TLV713P"} or {"standard": "IPC-2221B"}
    
    Returns:
        list of {text, score, citation} dicts. citation always includes
        source doc + page; standards include clause; datasheets include mpn.
    """
    if corpus is None:
        corpus = list(CORPUS_NAMES)
    chunks = []
    for c in corpus:
        retriever = build_retriever_for_corpus(c, filter=filter, top_k=top_k * 2)
        chunks.extend(retriever.invoke(query))
    # Cross-corpus rerank
    chunks = cohere_rerank(query, chunks, top_n=top_k)
    return [
        {
            "text": c.page_content,
            "score": c.metadata.get("rerank_score"),
            "citation": {
                "corpus": c.metadata["corpus"],
                "source": c.metadata["source"],
                "page": c.metadata.get("page"),
                "section": c.metadata.get("section"),
                "clause": c.metadata.get("clause"),
                "mpn": c.metadata.get("mpn"),
            },
        }
        for c in chunks
    ]
```

## `get_standard_clause` — direct lookup

When the agent already knows the clause it wants (e.g. "IPC-2221 §6.2"), skip retrieval and pull deterministically:

```python
@tool
def get_standard_clause(standard: str, clause_id: str) -> dict:
    """Direct lookup: e.g. ('IPC-2221B', '6.2'). Faster + more reliable than search."""
    chunk = standards_collection.get(
        where={"standard": standard, "clause": clause_id},
        limit=1,
    )
    return chunk
```

## Ingestion pipeline

```python
def ingest_pdf(path: str, corpus: str, force_reingest: bool = False):
    # 1. parse
    elements = LlamaParse(...).load_data(path)
    
    # 2. type-specific chunk
    chunks = chunk_by_type(elements, corpus)
    
    # 3. metadata enrichment
    for c in chunks:
        c.metadata["source"] = path
        c.metadata["corpus"] = corpus
        c.metadata["ingested_at"] = datetime.utcnow().isoformat()
        if corpus == "datasheets":
            c.metadata["mpn"] = extract_mpn(c)
        elif corpus == "standards":
            c.metadata["standard"], c.metadata["clause"] = extract_clause(c)
    
    # 4. embed + insert
    embeddings = voyage.embed([c.page_content for c in chunks])
    vectorstore.add_documents(chunks, embeddings=embeddings)
    
    # 5. update BM25 index
    rebuild_bm25(corpus)
```

## Citation contract
Every chunk returned by `rag_search` must have:
- `source` (the document path or URL)
- `page` (integer, where applicable)
- One of: `section`, `clause`, `mpn` (specific anchor within the doc)

Downstream agents reject any RAG-derived claim that lacks this. This is enforced in the verification workflow.

## Eval ideas
- **Retrieval @ K.** 30 hand-built questions with known-answer chunks. Measure recall@5.
- **Citation grounding.** For 30 agent outputs that cite RAG, audit whether the cited chunk actually supports the claim.
- **Cross-corpus routing.** 30 queries with known correct corpus; does the classifier pick it?
- **Re-ingest determinism.** Re-ingest the same doc twice; chunk IDs should be stable (matters for citation links to survive updates).

## Gotchas
- **Naive PDF parsing is the #1 quality killer.** A bad parse on a key datasheet ruins every downstream decision. Spend the time on the parsing/chunking pipeline.
- **Standards corpus needs the right edition.** IPC-2221A vs IPC-2221B differ. Tag `revision` and prefer latest in retrieval; let the verification agent be aware of mismatches.
- **Voyage embeddings have a context limit (16k tokens, generous).** OpenAI's is 8192. Chunk well under those limits.
- **BM25 needs to live in the same process** (it's not a network service). For multi-process deployment, consider OpenSearch with a sparse vector field instead.
- **Cache aggressively per session.** Same query twice in one session = same result; serve from cache.
