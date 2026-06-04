# 15 · Schematic Image → atopile Code (`sch2ato`)

> **Goal.** A tool that ingests an *image* of a schematic (PNG/JPG/PDF page — a scan, a screenshot, a datasheet figure, a KiCad/Altium export) that was **not** authored in atopile, and emits an equivalent `.ato` source file that rebuilds the same circuit in the atopile graph.
>
> **One-line verdict.** Feasible as a *human-in-the-loop assistant* that gets you 60–85% of the way on clean, single-sheet schematics. **Not** feasible (today) as a fully-automatic, trust-without-review converter for arbitrary real-world schematics. The blocker is not the `.ato` emission — that part is easy and we own all the machinery. The blocker is robust visual netlist extraction from pixels.

---

## 0. TL;DR for the impatient

- The pipeline is **image → netlist IR → `.ato`**. The second arrow is a solved problem (a few hundred lines). The first arrow is the entire difficulty.
- The right primitive is a **vision-language model (VLM)** doing structured extraction into an intermediate representation (IR), *not* classical CV symbol-matching. Classical CV (template matching, line tracing) is brittle across schematic styles; a VLM generalizes but hallucinates nets.
- Ship it as an **agent tool** inside the existing `src/atopile/server/agent/` harness, reusing the `AnthropicProvider` and tool-registry plumbing already described in `08_PROJECT_PLAN.md` and `06_ATOPILE_INTEGRATION.md`.
- Treat the output as a **draft for review**, surfaced as a side-by-side (image ⟷ generated `.ato` ⟷ `ato view` block diagram), never as a silent commit.
- **Complexity: High.** A useful v0 (single sheet, clean vector source, passives + a few ICs, ~80% net accuracy, human fixes the rest) is a **3–5 week** build. A "just works on anything" product is a **multi-quarter research effort** and arguably still open.

---

## 1. Why this is hard — decomposing the problem

Converting a schematic image is **four** sub-problems stacked, each with its own failure mode. The output is only as good as the weakest link, and errors compound multiplicatively, not additively.

| # | Sub-problem | What it produces | Why it's hard | Risk |
|---|---|---|---|---|
| 1 | **Symbol detection & classification** | Bounding boxes + type labels (resistor, cap, NPN, op-amp, the specific IC) | Schematic symbols are not standardized across IEC/ANSI/vendor styles; ICs are arbitrary boxes whose identity lives only in the text inside/beside them | Medium |
| 2 | **Pin / port localization** | For each symbol, the (x,y) and name of each pin | Pin numbers are tiny text, often rotated, sometimes implicit; multi-unit parts (gates A/B/C/D) share a symbol | High |
| 3 | **Net tracing (connectivity)** | Which pins are on the same net | Wires cross without connecting (hop vs junction), junction dots are ambiguous, **labels/net-names connect across the page with no drawn wire**, buses fan out | **Highest** |
| 4 | **Value & parameter OCR** | `10kΩ ±5%`, `100nF`, part numbers, ref designators | OCR on rotated/low-res/handwritten text; unit inference; tolerance often absent | Medium |

**Connectivity (Sub-problem 3) is the crux.** A schematic's meaning *is* its netlist. Get a symbol value wrong and you've mis-specced one part; get one net wrong and the circuit is electrically different and silently incorrect. There is no local check that catches a swapped net — it requires understanding intent. This is exactly where VLMs hallucinate most, because "plausible connection" and "actual connection" look identical in latent space.

### Why classical CV alone loses

Template matching + Hough-line tracing works on a *single, clean, known* schematic style and collapses on:
- hand-drawn or scanned-with-skew schematics,
- off-page connectors and hierarchical labels (connectivity with **no drawn wire**),
- vendor symbol libraries it's never seen,
- raster artifacts (anti-aliasing, JPEG ringing) that break line continuity.

### Why VLM alone loses

A frontier VLM (Claude with vision) reads symbols, text, and topology *holistically* and degrades gracefully on novel styles — but it **confabulates nets** on dense sheets, miscounts pins on big ICs, and silently drops components in busy regions. It is excellent at the *semantic* layer (this is an LDO, this cap is decoupling VCC) and unreliable at the *exhaustive-connectivity* layer.

**Conclusion:** the realistic architecture is **VLM-primary, CV-assist, human-verify** — and the product framing must be "draft + review," not "import button."

---

## 2. The one structural advantage we have

The **emit** half is trivial *because we control the target language and already have the graph + visualization*:

1. **`.ato` is a clean compile target.** It's declarative — `new`, `~`, `~>`, assignments. No layout, no coordinates, no procedural logic to synthesize. A netlist IR maps almost 1:1 onto `.ato` statements (see §4 emitter).
2. **The compiler is the validator.** Anything we emit gets type-checked, solved, and netlisted by the existing pipeline for free. Connect `I2C ~ ElectricPower` and it errors — a cheap, automatic sanity gate on the VLM's output.
3. **`ato view` already renders the instance graph** (`src/atopile/cli/view.py`, localhost:8765). We get an instant **visual diff surface**: put the source image next to `ato view` of the generated code and a human spots a wrong net in seconds.
4. **The stdlib gives us a typed vocabulary** (`Resistor`, `Capacitor`, `ElectricPower`, `I2C`, `ElectricLogic`, …, see `ato-language` skill) so the VLM extracts into a *constrained* schema instead of free-form text — which sharply reduces hallucination.
5. **A short-circuit exists for KiCad inputs.** If the source is actually a `.kicad_sch` file (not a flat image), skip vision entirely: the Zig sexp engine already has a full typed `KicadSch` model (`src/faebryk/core/zig/src/sexp/kicad/schematic.zig`, Python stubs in `gen/sexp/schematic.pyi`) — see `13_KICAD_SCH_AND_FRONTEND_FILES.md §1.1`. That path is deterministic and high-accuracy and should be a **separate, prioritized track**.

> **Design rule:** the IR is the contract. Both the VLM extractor and the deterministic KiCad parser produce the *same* `SchematicIR`, and a single emitter turns IR into `.ato`. This lets us ship the easy/deterministic path first and swap in the hard/vision path behind the same interface.

---

## 3. Architecture

```
                          ┌──────────────────────────────────────────┐
   image (png/jpg/pdf) ──▶│  Pre-process: deskew, upscale, page-split │
                          │  PDF → per-page raster; tile large sheets │
                          └───────────────────┬──────────────────────┘
                                              │
            ┌─────────────────────────────────┴─────────────────────────────┐
            ▼ (vision path)                                                  ▼ (deterministic path)
  ┌───────────────────────┐                                        ┌───────────────────────┐
  │ VLM structured extract │   optional CV assist:                 │ .kicad_sch via Zig     │
  │ → SchematicIR (JSON)   │◀─ symbol boxes, line graph,           │ sexp KicadSch model    │
  │  components/pins/nets  │   junction/label detection            │ → SchematicIR          │
  └──────────┬─────────────┘   (grounding hints to reduce halluc.) └───────────┬───────────┘
             │                                                                  │
             └───────────────────────────┬──────────────────────────────────────┘
                                         ▼
                          ┌──────────────────────────────┐
                          │  SchematicIR validation       │  net sanity, dangling pins,
                          │  + part resolution (lib match) │  power/ground heuristics
                          └───────────────┬───────────────┘
                                          ▼
                          ┌──────────────────────────────┐
                          │  .ato emitter                 │  IR → imports + module +
                          │                               │  new/~/~> + assignments
                          └───────────────┬───────────────┘
                                          ▼
                  ┌────────────────────────────────────────────┐
                  │  ato build (compile + solve)  ── validates  │
                  │  ato view                     ── visual diff │
                  └────────────────────────────────────────────┘
                                          ▼
                          ┌──────────────────────────────┐
                          │  Human review: image ⟷ .ato   │  accept / correct / re-prompt
                          │  ⟷ block diagram, in webview  │
                          └──────────────────────────────┘
```

### 3.1 The Intermediate Representation (`SchematicIR`)

The IR is deliberately netlist-shaped and decoupled from `.ato` syntax:

```python
# src/atopile/sch2ato/ir.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class Pin:
    name: str                       # "1", "2", "VCC", "GND", "SCL"
    net: str                        # net id this pin attaches to (key into IR.nets)
    confidence: float = 1.0         # extractor confidence, drives review highlighting

@dataclass
class Component:
    ref: str                        # ref designator from the sheet: "R1", "U3", "C7"
    kind: str                       # stdlib type or vendor hint: "Resistor", "Capacitor",
                                    #   "ElectricPower", "IC:ESP32-C3", ...
    value: str | None = None        # "10kohm +/- 5%", "100nF", None for ICs
    pins: list[Pin] = field(default_factory=list)
    part_number: str | None = None  # MPN if printed on the sheet
    bbox: tuple[int, int, int, int] | None = None   # source-image box, for review overlay
    confidence: float = 1.0

@dataclass
class Net:
    id: str                         # "VCC", "GND", "N$4", a label, or a synthesized id
    name: str | None = None         # human/label name if one was printed
    kind: Literal["power", "ground", "signal", "bus"] = "signal"

@dataclass
class SchematicIR:
    components: list[Component] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    source: str = ""                # provenance: file path / page
    warnings: list[str] = field(default_factory=list)   # extractor self-reported doubts
```

---

## 4. Code skeleton

> Skeleton, not production. Lives under a new `src/atopile/sch2ato/` package and registers a tool in the existing agent harness (`src/atopile/server/agent/tools.py`). Vision uses the `AnthropicProvider` already planned in `11_ANTHROPIC_PROVIDER.md`.

### 4.1 VLM extractor — image → IR

```python
# src/atopile/sch2ato/extract.py
import base64, json
from pathlib import Path
from anthropic import Anthropic
from .ir import SchematicIR, Component, Net, Pin

# The schema we force the model to fill. Constraining to stdlib kinds is the single
# biggest hallucination-reducer: the model picks from a vocabulary, not free text.
STDLIB_KINDS = [
    "Resistor", "Capacitor", "CapacitorPolarized", "Inductor", "Diode", "LED",
    "MOSFET", "BJT", "Crystal", "Fuse", "ElectricPower", "Net", "TestPoint",
    "IC",   # generic: identity carried in part_number / kind suffix
]

EXTRACT_SYSTEM = """You are an expert electronics engineer transcribing a schematic IMAGE
into a structured netlist. Be exhaustive and conservative:
- List EVERY component you can see, with its printed reference designator.
- For each component pin, name the net it connects to. Two pins on the SAME net MUST
  share an identical net id. Named labels (VCC, GND, SDA, 3V3) ARE nets even with no
  drawn wire — pins under the same label are connected.
- A crossing of wires is NOT a connection unless there is a junction dot.
- If unsure about a connection, LOWER its pin confidence and add a line to `warnings`.
  Never invent a connection to make the circuit "look complete".
Return ONLY JSON matching the provided schema."""

def _img_block(path: Path) -> dict:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "source": {
        "type": "base64", "media_type": media,
        "data": base64.standard_b64encode(path.read_bytes()).decode(),
    }}

def extract_ir(image: Path, client: Anthropic, model: str = "claude-opus-4-8") -> SchematicIR:
    msg = client.messages.create(
        model=model, max_tokens=8000, system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": [
            _img_block(image),
            {"type": "text", "text":
                f"Allowed component kinds: {STDLIB_KINDS}. "
                "Transcribe the schematic into the SchematicIR JSON schema."},
        ]}],
        # In practice: pass a JSON schema via tool-use / structured output instead of
        # trusting free-form JSON. Omitted here for brevity.
    )
    raw = json.loads(_only_json(msg.content[0].text))
    return SchematicIR(
        components=[Component(**{**c, "pins": [Pin(**p) for p in c.get("pins", [])]})
                    for c in raw["components"]],
        nets=[Net(**n) for n in raw.get("nets", [])],
        source=str(image),
        warnings=raw.get("warnings", []),
    )

def _only_json(text: str) -> str:
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1]
```

### 4.2 Validation & part resolution — IR → checked IR

```python
# src/atopile/sch2ato/validate.py
from .ir import SchematicIR

POWER_HINTS = {"VCC", "VDD", "3V3", "5V", "VBAT", "VIN", "VOUT"}
GROUND_HINTS = {"GND", "VSS", "AGND", "DGND", "0V"}

def classify_nets(ir: SchematicIR) -> SchematicIR:
    """Heuristically tag power/ground nets so the emitter can use ElectricPower."""
    for net in ir.nets:
        up = (net.name or net.id).upper()
        if up in GROUND_HINTS: net.kind = "ground"
        elif any(up.startswith(h) for h in POWER_HINTS): net.kind = "power"
    return ir

def sanity_checks(ir: SchematicIR) -> list[str]:
    """Cheap structural lint the VLM output must pass before we trust it."""
    problems: list[str] = []
    net_ids = {n.id for n in ir.nets}
    pin_count: dict[str, int] = {}
    for c in ir.components:
        for p in c.pins:
            pin_count[p.net] = pin_count.get(p.net, 0) + 1
            if p.net not in net_ids:
                problems.append(f"{c.ref}.{p.name} → unknown net '{p.net}'")
    for net, n in pin_count.items():
        if n == 1:
            problems.append(f"net '{net}' has only one pin (dangling?)")
    # Surface every low-confidence pin so the reviewer's eye goes straight to it.
    for c in ir.components:
        for p in c.pins:
            if p.confidence < 0.6:
                problems.append(f"LOW-CONF {c.ref}.{p.name} ~ {p.net} ({p.confidence:.2f})")
    return problems
```

### 4.3 Emitter — IR → `.ato`

```python
# src/atopile/sch2ato/emit.py
from .ir import SchematicIR, Component

STDLIB_IMPORT = {  # kind → import line
    "Resistor": "Resistor", "Capacitor": "Capacitor",
    "CapacitorPolarized": "CapacitorPolarized", "Inductor": "Inductor",
    "Diode": "Diode", "LED": "LED", "MOSFET": "MOSFET", "BJT": "BJT",
    "Crystal": "Crystal", "Fuse": "Fuse", "ElectricPower": "ElectricPower",
    "Net": "Net", "TestPoint": "TestPoint",
}

def _inst_name(c: Component) -> str:
    return c.ref.lower() if c.ref else c.kind.lower()

def emit_ato(ir: SchematicIR, module_name: str = "ImportedSchematic") -> str:
    imports = sorted({STDLIB_IMPORT[c.kind] for c in ir.components
                      if c.kind in STDLIB_IMPORT})
    lines: list[str] = []
    lines.append('#pragma experiment("BRIDGE_CONNECT")')
    lines.append("")
    for imp in imports:
        lines.append(f"import {imp}")
    lines.append("")
    lines.append(f"module {module_name}:")
    lines.append(f'    """Auto-generated from {ir.source}. REVIEW BEFORE USE."""')

    # 1. instantiate components
    for c in ir.components:
        if c.kind in STDLIB_IMPORT:
            lines.append(f"    {_inst_name(c)} = new {STDLIB_IMPORT[c.kind]}")
        else:  # unresolved IC → placeholder with a TODO the human resolves
            lines.append(f"    # TODO resolve part: {c.ref} {c.kind} {c.part_number or ''}")
            lines.append(f"    {_inst_name(c)} = new Net  # placeholder for {c.ref}")

    # 2. declare named signal nets (so connections read well)
    sig_nets = [n for n in ir.nets if n.kind in ("signal", "bus")]
    for n in sig_nets:
        lines.append(f"    {_san(n.id)} = new Net")

    # 3. connections: group pins by net, wire each pin to its net's part_of
    by_net: dict[str, list[tuple[Component, str]]] = {}
    for c in ir.components:
        for p in c.pins:
            by_net.setdefault(p.net, []).append((c, p.name))
    for net_id, members in by_net.items():
        for (c, pin) in members:
            lines.append(f"    {_inst_name(c)}.{_san(pin)} ~ {_san(net_id)}.part_of")

    # 4. values
    for c in ir.components:
        if c.value:
            lines.append(f"    {_inst_name(c)}.value = {c.value}")
    return "\n".join(lines) + "\n"

def _san(s: str) -> str:
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in s)
```

### 4.4 Agent tool registration & top-level driver

```python
# src/atopile/sch2ato/__init__.py
from pathlib import Path
from anthropic import Anthropic
from .extract import extract_ir
from .validate import classify_nets, sanity_checks
from .emit import emit_ato

def schematic_image_to_ato(image_path: str, module_name: str = "ImportedSchematic"):
    """Tool entrypoint: returns generated .ato + the lint findings for review."""
    client = Anthropic()
    ir = classify_nets(extract_ir(Path(image_path), client))
    problems = sanity_checks(ir) + ir.warnings
    ato_src = emit_ato(ir, module_name)
    return {
        "ato": ato_src,
        "review_required": problems,         # never auto-commit if non-empty
        "components_found": len(ir.components),
        "nets_found": len(ir.nets),
    }

# registered into src/atopile/server/agent/tools.py alongside the existing tools,
# exposed to the webview so the result lands in a side-by-side review pane.
```

---

## 5. Implementation plan (phased)

Vertical slices, mirroring the milestone discipline in `08_PROJECT_PLAN.md`. Each phase is independently demoable.

| Phase | Scope | Done definition | Est. |
|---|---|---|---|
| **0 · Spike** | Hand-build IR for 1 known schematic; run emitter; `ato build` it | A hand-written IR round-trips to compilable `.ato`. Proves the easy half end-to-end. | 2–3 d |
| **1 · Deterministic KiCad path** | `.kicad_sch` → IR via Zig sexp `KicadSch` model → emitter | Any KiCad schematic converts with ~100% net fidelity. Ships real value *with zero vision risk*. | 1–1.5 wk |
| **2 · VLM extractor v0** | Image → IR for *single-sheet, clean vector* schematics; passives + ≤3 ICs | On a 15-schematic eval set: ≥90% components found, ≥80% nets correct, all low-conf flagged | 1.5–2 wk |
| **3 · Review UX** | Webview side-by-side: source image ⟷ generated `.ato` ⟷ `ato view`; click a flagged net to highlight bbox | Reviewer can accept/correct a draft in <5 min for a 20-part sheet | 1 wk |
| **4 · CV grounding assist** | Symbol-box + junction/label detection fed to the VLM as hints; reduces dropped parts & phantom nets | +10pp net accuracy on the eval set vs Phase 2 | 2–3 wk |
| **5 · Hierarchy & multi-page** | PDF multi-sheet, off-page connectors, hierarchical labels stitched across pages | A 3-sheet design converts into a multi-module `.ato` project | 2–3 wk |
| **6 · Part resolution** | MPN/value → real library or `is_atomic_part` (tie into existing part picking) | ICs resolve to real parts or clean atomic-part stubs, not `Net` placeholders | 1–2 wk |

**To first genuinely-useful artifact:** Phase 1 (KiCad, deterministic) in ~2 weeks. **To the actual ask (images):** Phases 0–3 in ~4–5 weeks for a review-grade single-sheet converter.

### Build vs. defer
- **Build now:** Phases 0–2 (the IR contract + KiCad path + VLM v0). Highest value per unit risk.
- **Defer/research:** Phases 4–5. Diminishing returns; this is where it turns into an open CV/document-understanding problem. Only invest if real usage shows single-sheet vision isn't enough.

---

## 6. Evaluation strategy

You cannot tune what you can't score. Build the eval set **before** Phase 2.

- **Corpus:** 15–30 schematics with *ground-truth netlists* (export KiCad/Altium designs → known netlist; render to image; that image is the test input, the netlist is the answer key). This gives free, exact labels at scale.
- **Metrics:**
  - **Component recall/precision** (did we find each part, right type?).
  - **Net accuracy** = edge-level F1 over the pin-to-pin connectivity graph. *This is the headline number.* A "net" is correct only if its exact pin set matches.
  - **Compile rate** (does emitted `.ato` build?) — necessary but weak; a circuit can compile and be wrong.
  - **Human-correction time** — the real product metric: minutes to make the draft correct.
- **Gate:** no auto-commit while net-accuracy < ~0.95; below that it's strictly draft-for-review. Be explicit in the UI about which nets are model-guessed vs. label-derived.

---

## 7. Risks & honest caveats

| Risk | Severity | Mitigation |
|---|---|---|
| VLM hallucinates plausible-but-wrong nets | **High** | Per-pin confidence + lint + human review; never silent-commit; compile gate |
| Big ICs: miscounted/mislabeled pins | High | Phase 6 part resolution from MPN → real footprint pinout (authoritative pin list) |
| Connectivity via labels/off-page (no wire) | High | Explicit prompt rules + label-detection in Phase 4; Phase 5 for cross-sheet |
| Dense/low-res/hand-drawn scans | Medium | Pre-process (deskew/upscale/tile); set expectations — this is the worst case |
| Vendor-specific symbols unseen by model | Medium | VLM generalizes better than templates; flag low confidence |
| Value/tolerance absent on sheet | Low | Emit value with a `# TODO tolerance` — passives w/o tolerance match no real part (stdlib invariant #3) |
| "Looks done" false confidence | Medium | UX must foreground the diff and the warnings, not hide them |

### What this tool is and isn't
- **Is:** a force-multiplier that turns a 2-hour manual transcription into a 15-minute review-and-fix. A deterministic, high-fidelity importer for KiCad sources.
- **Isn't:** a trustworthy one-click "image → correct circuit" black box. Net errors are silent and electrically meaningful; a human in the loop is mandatory for the foreseeable future.

---

## 8. Complexity & feasibility verdict

| Dimension | Rating | Note |
|---|---|---|
| `.ato` emission | **Trivial** | We own the language; IR→`.ato` is ~200 LoC; compiler validates for free |
| KiCad `.kicad_sch` import | **Low** | Zig sexp `KicadSch` model already exists (`13_KICAD…md §1.1`); deterministic |
| Single-sheet vision (clean vector) | **Medium-High** | VLM does it at draft quality; needs eval + review UX |
| Arbitrary real-world images (scans, hierarchy, dense) | **Very High / partly open** | Document-understanding research territory; long tail never fully closes |
| Overall, for a **review-grade single-sheet** tool | **High but tractable** | ~4–5 weeks, mostly UX + eval, not unsolved research |
| Overall, for a **trust-without-review** tool | **Infeasible today** | Net-accuracy ceiling + silent-error severity |

**Recommendation.** Build it, scoped as a **human-in-the-loop draft generator** behind the existing agent harness. Sequence: **IR contract → deterministic KiCad path → VLM single-sheet → review UX.** Ship value at Phase 1 (KiCad) within ~2 weeks; deliver the actual image ask at draft quality by ~week 5. Hold the line on "review required," and gate any future automation on the net-accuracy metric from §6.

---

## 9. Cross-references

- `13_KICAD_SCH_AND_FRONTEND_FILES.md` — the Zig `KicadSch` sexp model (deterministic input path) and the frontend webview message-bus constraints (relevant to the review UX in Phase 3).
- `11_ANTHROPIC_PROVIDER.md` — the vision-capable provider the extractor calls.
- `08_PROJECT_PLAN.md` — milestone/vertical-slice discipline this plan follows.
- `06_ATOPILE_INTEGRATION.md` — where the tool plugs into the agent tool registry without forking upstream UI.
- `.claude/skills/ato-language/SKILL.md` — the stdlib type vocabulary the IR extracts into and the emitter targets.
- `src/atopile/cli/view.py` (`ato view`) — the block-diagram render reused as the visual-diff surface.
</content>
</invoke>
