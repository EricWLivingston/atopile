# RAG validation set — datasheets corpus (50 questions)

> **Status: approved** (30-question core approved by user 2026-06-10; expanded to 50 on
> request — +17 implementation-focused conceptual questions, +3 spec lookups — with
> auto-approval). Mirrored verbatim to `src/ee_agent_rag/eval/datasets/datasheets.jsonl`;
> the recall@5 gate is ≥ 0.80, i.e. ≥ 40/50. Every question is grounded in text actually
> present in the PDF (verified via local pdfminer extraction — no paid calls used).
>
> **Eval result (2026-06-10): recall@5 = 0.98 (49/50) — PASS.** Sole miss is #30
> (TPS61030 inductor value): the answer sentence is dropped by LlamaParse (equation-region
> parse limitation), not a retrieval failure — see `RAG_TUNING_SUMMARY.md`.

## How a question is scored

A question **passes** if at least one of the top-5 retrieved chunks satisfies **every**
expectation listed for it:

| Key | Check |
|---|---|
| `source` (`must_source_contain`) | citation source path contains this substring (case-insensitive) — proves the right *document* was retrieved. New eval-runner key, added in Phase B. |
| `mpn` (`must_contain_mpn`) | citation MPN equals this exactly |
| `section` (`must_contain_section`) | citation section contains this substring (ci) |
| `text` (`must_contain_text`) | chunk text contains this substring (ci) — proves the right *chunk*, not just the right doc |

Design choices, deliberately:
- **`source` is the primary check** — deterministic and robust. MPN regex and section
  names depend on parse output, so they're used sparingly where they're stable.
- **`text` needles are format-stable strings** (bare numbers like `2.048`, words like
  `lockstep`) that survive whatever table formatting LlamaParse emits. No needles with
  unit-spacing ambiguity ("3 MHz" vs "3MHz").
- **Query-type mix** mirrors real agent usage and exercises each retrieval stage:
  ~14 spec lookups, ~9 conceptual paraphrases (no shared keywords → tests dense/semantic),
  ~4 exact-identifier lookups (tests BM25/sparse), ~3 part-numbering/ordering.

## The questions

### CD0603-B0340R — Bourns Schottky chip diode
| # | Query | Expectations | Why |
|---|---|---|---|
| 1 | What is the maximum repetitive peak reverse voltage of the CD0603-B0340R Schottky diode? | source `CD0603` · section `Absolute Maximum` | Spec lookup against the abs-max table (VRRM = 40 V). |
| 2 | Which small leadless chip diode is suitable for high frequency rectification in switch mode power supplies? | source `CD0603` | Conceptual, no part number — pure dense-retrieval test (Features/Applications text). |

### CM02X5R104M06AH — Kyocera MLCC (catalog-style doc)
| # | Query | Expectations | Why |
|---|---|---|---|
| 3 | CM02X5R104M06AH capacitance and voltage rating | source `CM02X5R` | Exact-identifier query — BM25 must dig the full part number out of a ratings table. Hardest sparse test in the set. |
| 4 | How do I decode a Kyocera multilayer ceramic chip capacitor part number? | source `CM02X5R` · text `X5R` | Conceptual; targets the "How to Order" part-number explanation. |

### ERJ2GEJ102X — Panasonic thick-film chip resistor
| # | Query | Expectations | Why |
|---|---|---|---|
| 5 | What qualification standard are Panasonic ERJ thick film chip resistors rated to for automotive use? | source `ERJ2GE` · text `AEC-Q200` | Spec lookup with a stable text needle. |
| 6 | How is the resistance value encoded in the ERJ resistor part number? | source `ERJ2GE` | Part-numbering question — common real agent task when picking passives. |

### LQG15HN1N6S02D — Murata RF chip inductor (catalog-style doc)
| # | Query | Expectations | Why |
|---|---|---|---|
| 7 | What is the rated current of the LQG15HN 1.6 nH chip inductor? | source `LQG15HN` | Spec lookup in a dense catalog-style ratings table (300 mA) — tests whether the chunker keeps giant tables usable. |
| 8 | Which Murata chip inductor series is designed for high frequency mobile phone circuits like PA and VCO? | source `LQG15HN` | Conceptual paraphrase of the Features/Applications text. |

### MC100EP51DTG — onsemi ECL D flip-flop
| # | Query | Expectations | Why |
|---|---|---|---|
| 9 | What is the typical propagation delay of the MC100EP51 differential clock D flip-flop? | source `MC100EP51` | Spec lookup (350 ps typ). |
| 10 | What happens to the EP51 flip-flop clock inputs when they are left open? | source `MC100EP51` | Behavioral question (clamp circuitry, CLK pulled to VEE) — tests retrieval of prose, not tables. |

### MCP4728 — Microchip quad 12-bit DAC with EEPROM
| # | Query | Expectations | Why |
|---|---|---|---|
| 11 | What is the internal voltage reference of the MCP4728 DAC? | source `MCP4728` · text `2.048` | Spec lookup with stable numeric needle. |
| 12 | What output voltage range does the MCP4728 provide using the internal reference with gain setting of 2? | source `MCP4728` · text `4.096` | Spec lookup requiring the right *row* of the feature description. |
| 13 | Which quad DAC stores its settings in nonvolatile memory so outputs are restored immediately after power-up? | source `MCP4728` · mpn `MCP4728` | Conceptual paraphrase (no "MCP4728", no "EEPROM" keyword match required) + MPN-metadata check (needs the Phase-B MPN pattern extension). |

### NCP6334B — onsemi 3 MHz / 2 A synchronous buck
| # | Query | Expectations | Why |
|---|---|---|---|
| 14 | What is the switching frequency of the NCP6334B buck converter? | source `NCP6334` | Spec lookup (3 MHz). |
| 15 | What input voltage range does the NCP6334B operate from? | source `NCP6334` · text `5.5` | Spec lookup (2.3–5.5 V) with stable needle. |

### NVT2008BQ — NXP bidirectional level translator
| # | Query | Expectations | Why |
|---|---|---|---|
| 16 | How can I translate an I2C bus between 1.8 V and 5 V without a direction pin? | source `NVT2008` | Conceptual, application-shaped — exactly how the agent would really ask. |
| 17 | What is the ON-state resistance of the NVT2008 translator switch? | source `NVT2008` | Spec lookup (3.5 Ω Ron). |

### Si2366DS — Vishay 30 V N-channel MOSFET
| # | Query | Expectations | Why |
|---|---|---|---|
| 18 | What is the maximum on-resistance of the Si2366DS at VGS = 4.5 V? | source `SI2366` | Spec lookup in the Product Summary / EC table (0.042 Ω). |
| 19 | Which 30 V N-channel MOSFET in SOT-23 works as a load switch for portable applications? | source `SI2366` | Conceptual part-selection query. |

### SP3088E — MaxLinear/Exar RS-485/RS-422 transceiver
| # | Query | Expectations | Why |
|---|---|---|---|
| 20 | What is the maximum data rate of the SP3088E transceiver? | source `SP3088` | Spec lookup (20 Mbps; the family table lists 115 kbps/500 kbps variants — tests precision against sibling parts in the same doc). |
| 21 | What is the receiver output state of the SP3088E when the bus lines are left undriven or shorted? | source `SP3088` | Behavioral (advanced failsafe → logic high) — prose retrieval. |

### SPX3819 — MaxLinear 500 mA low-noise LDO
| # | Query | Expectations | Why |
|---|---|---|---|
| 22 | What is the dropout voltage of the SPX3819 at full load? | source `SPX3819` · text `340` | Spec lookup with stable needle (340 mV). |
| 23 | SPX3819 quiescent current | source `SPX3819` | Terse keyword-style query (90 µA) — how engineers actually type. |

### LMV324-N — TI low-voltage rail-to-rail op amp
| # | Query | Expectations | Why |
|---|---|---|---|
| 24 | What is the gain-bandwidth product of the LMV324 op amp? | source `lmv324` | Spec lookup (1 MHz). |
| 25 | Which low-voltage op amp family has rail-to-rail output and an input common-mode range that includes ground? | source `lmv324` | Conceptual part-selection paraphrase. |

### RM46L852 — TI Hercules safety MCU (191-page doc)
| # | Query | Expectations | Why |
|---|---|---|---|
| 26 | What is the maximum system clock frequency of the RM46L852 microcontroller? | source `rm46l852` · text `220` | Spec lookup; the 191-page doc is the stress test for chunking/retrieval depth. |
| 27 | How much program flash and RAM does the RM46L852 provide? | source `rm46l852` | Memory-size lookup (1.25 MB flash / 192 KB RAM). |
| 28 | Which microcontroller has dual CPUs running in lockstep for safety-critical applications? | source `rm46l852` · text `lockstep` | Conceptual safety-feature query with stable needle. |

### TPS61030 — TI synchronous boost converter
| # | Query | Expectations | Why |
|---|---|---|---|
| 29 | What is the typical quiescent current of the TPS61030 boost converter? | source `tps61030` · mpn `TPS61030` | Spec lookup (20 µA typ) + MPN-metadata check (TPS pattern already exists in `enrich.py`). |
| 30 | Which boost converter can deliver 1 A at 5 V from an input as low as 1.8 V? | source `tps61030` | Conceptual part-selection paraphrase of the headline capability. |

## Implementation-focused additions (#31–50)

Per review feedback: more conceptual, high-level lookups with emphasis on **implementing
the part in a design** (external components, configuration, behavior in-circuit), plus a
few more direct spec lookups. All grounded in each PDF's Application/Implementation
sections (verified present via pdfminer).

### Implementation / application questions
| # | Query | Expectations | Why |
|---|---|---|---|
| 31 | What inductor value is recommended for a typical TPS61030 application? | source `tps61030` · text `6.8` | Implementation: the Inductor Selection section recommends 6.8 µH. |
| 32 | What input capacitor is recommended to improve transient and EMI behavior of the TPS61030? | source `tps61030` | Implementation: input-cap guidance (≥10 µF) from the app section. |
| 33 | How do I set the output voltage of an adjustable TPS61030 boost converter? | source `tps61030` · mpn `TPS61030` | Implementation: external feedback resistor divider on FB — plus MPN-citation gate. |
| 34 | How does the NCP6334B buck converter behave at light load currents? | source `NCP6334` · text `PFM` | Implementation/behavior: automatic PFM mode in discontinuous conduction. |
| 35 | What should be connected to the SPX3819 bypass pin for low-noise operation? | source `SPX3819` | Implementation: reference bypass cap (10 nF) for low-noise output. |
| 36 | What output capacitor considerations keep the SPX3819 LDO stable? | source `SPX3819` | Implementation: output cap/ESR stability discussion. |
| 37 | How should the NVT2008 enable pin be connected in an always-on application? | source `NVT2008` | Implementation: EN tied to Vref(B) via pull-up (typ. 200 kΩ) — from "Application design-in information". |
| 38 | Do I need pull-up resistors on the bus side of the NVT2008 when devices are open-drain? | source `NVT2008` | Implementation: open-drain systems require pull-ups to Vpu(D). |
| 39 | How can the LMV324 op amp drive a large capacitive load without oscillating? | source `lmv324` · text `isolation` | Implementation: resistive-isolation technique from §8.3.1 Capacitive Load Tolerance. |
| 40 | Why does direct capacitive loading cause an op amp buffer to ring or oscillate? | source `lmv324` · text `phase` | Conceptual: phase-margin reduction explanation — pure prose retrieval. |
| 41 | How many SP3088E transceivers can share one RS-485 bus? | source `SP3088` · text `256` | Implementation: 1/8th unit load → 256 transceivers. |
| 42 | What protection does the SP3088E provide when a board is hot-plugged into a live backplane? | source `SP3088` | Implementation: Hot Swap glitch protection on control inputs. |
| 43 | How do I update all four MCP4728 DAC outputs simultaneously? | source `MCP4728` · text `LDAC` | Implementation: LDAC pin synchronizes output updates. |
| 44 | What happens to the MCP4728 outputs in power-down mode? | source `MCP4728` | Implementation/behavior: outputs present a known low/medium/high resistive load. |
| 45 | What supply configurations let the MC100EP51 operate in PECL and NECL modes? | source `MC100EP51` · text `PECL` | Implementation: PECL VCC = 3.0–5.5 V / NECL VEE = −3.0 to −5.5 V. |
| 46 | What core and I/O supply voltages does the RM46L852 require? | source `rm46l852` · text `1.32` | Implementation: VCC 1.14–1.32 V, VCCIO 3.0–3.6 V. |
| 47 | How does the maximum forward current of the CD0603 Schottky diode change with ambient temperature? | source `CD0603` | Implementation/derating: Forward Current Derating Curve. |

### Additional spec lookups
| # | Query | Expectations | Why |
|---|---|---|---|
| 48 | What is the total gate charge of the Si2366DS MOSFET? | source `SI2366` | Spec lookup (Qg 3.2 nC typ) — gate-drive design input. |
| 49 | What is the minimum self-resonant frequency of the LQG15HN 1.6 nH inductor? | source `LQG15HN` · text `6000` | Spec lookup deep in the catalog ratings table (6000 MHz). |
| 50 | What is the rated power of a 0402 size ERJ thick film chip resistor? | source `ERJ2GE` | Spec lookup in the power-rating table. |

## Coverage summary

- **All 14 datasheets covered** by the 30-question core (2 each; 3 for MCP4728/RM46L852
  minus one to land on 30), plus 20 additions weighted toward the application-rich docs
  (TPS61030 ×3, MCP4728/NVT2008/SPX3819/LMV324/SP3088 ×2 each).
- Final mix: ~17 spec lookups, ~17 implementation/application questions, ~9 conceptual
  paraphrases, ~4 exact-identifier queries, ~3 part-numbering/ordering.
- 4 questions also gate **MPN metadata extraction** (#13, #29, #33) and **section
  metadata** (#1) so citation quality is tested, not just text retrieval.
- Catalog-style docs (Murata, Kyocera) are intentionally included — they parse worst and
  will likely drive the tuning; that's representative of real corpora.
- Nothing here is tuned *to* the pipeline: questions were written from the PDFs' own text
  before any ingestion, and conceptual queries deliberately avoid the documents' exact
  wording where the type calls for it.
