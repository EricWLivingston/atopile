"""Unit tests for the table-fidelity scanner (synthetic markdown, no corpus)."""

from ee_agent_rag.eval.table_fidelity import scan_markdown

_SPAN_FLATTENED = """\
## Absolute Maximum Ratings

| Parameter | Symbol | CD0603-B0240R | CD0603-B0340R | Unit |
|---|---|---|---|---|
| Peak Reverse Voltage | VRRM | 40 |  | V |
| Forward Current | IF(AV) | 200 | 300 | mA |
| Surge Current | IFSM |  | 2 | A |
"""

_CLEAN = """\
| Parameter | Symbol | CD0603-B0240R | CD0603-B0340R | Unit |
|---|---|---|---|---|
| Forward Current | IF(AV) | 200 | 300 | mA |
"""

_NO_PART_COLUMNS = """\
| Parameter | Min | Typ | Max | Unit |
|---|---|---|---|---|
| Quiescent Current |  | 20 |  | uA |
"""

_RAGGED = """\
| Symbol | Pin NVT2008BQ | Pin NVT2010BQ | Description |
|---|---|---|---|
| GND | 1 | 1 | ground |
| A2 | 4 | 1 |
"""


def test_flattened_span_rows_are_flagged():
    findings = scan_markdown(_SPAN_FLATTENED)
    spans = [f for f in findings if f.kind == "span_suspect"]
    assert len(spans) == 2  # VRRM and IFSM rows; IF(AV) row is fully populated
    assert any("VRRM" in f.row for f in spans)
    assert any("IFSM" in f.row for f in spans)


def test_fully_populated_part_columns_are_clean():
    assert scan_markdown(_CLEAN) == []


def test_min_typ_max_tables_are_not_part_columns():
    # legitimately sparse min/typ/max cells must not be flagged
    assert scan_markdown(_NO_PART_COLUMNS) == []


def test_ragged_rows_are_flagged():
    findings = scan_markdown(_RAGGED)
    assert [f.kind for f in findings] == ["ragged_row"]
    assert "A2" in findings[0].row


def test_non_table_text_is_ignored():
    assert scan_markdown("# heading\nplain prose | with a pipe\n") == []


# The CD0603 EC-table defect: split/merged body cells under one header column shift
# every later value one column left — Typ values land under Min, Max comes out empty.
_SHIFTED_EC = """\
| Parameter | Symbol | Test Condition | Min. | Typ. | Max. | Unit |
|---|---|---|---|---|---|---|
| Forward Voltage | VF | IF = 50 mA | 0.35 |  |  | V |
| Forward Voltage | VF | IF = 100 mA | 0.38 |  |  | V |
| Forward Voltage | VF | IF = 200 mA | 0.43 | 0.5 |  | V |
| Reverse Current | IRRM | VR = 10 V | 0.5 | 1 |  | μA |
| Reverse Current | IRRM | 3 | 50 |  |  | μA |
"""

# The corrected premium parse of the same table: Min mostly empty, Typ filled.
_CORRECT_EC = """\
| Parameter | Symbol | Test Condition | Min. | Typ. | Max. | Unit |
|---|---|---|---|---|---|---|
| Forward Voltage | VF | IF = 50 mA |  | 0.35 |  | V |
| Forward Voltage | VF | IF = 100 mA |  | 0.38 |  | V |
| Forward Voltage | VF | IF = 200 mA | 0.43 | 0.5 |  | V |
| Reverse Current | IRRM | VR = 10 V |  | 0.5 | 1 | μA |
"""

# Raw cached premium parses replicate the section title into every header cell.
_SHIFTED_EC_POLLUTED_HEADER = _SHIFTED_EC.replace(
    "| Parameter | Symbol | Test Condition | Min. | Typ. | Max. | Unit |",
    "| EC<br/>Parameter | EC<br/>Symbol | EC<br/>Test Condition | EC<br/>Min. "
    "| EC<br/>Typ. | EC<br/>Max. | EC<br/>Unit |",
)

_SHORT_SPARSE = """\
| Parameter | Min | Typ | Max | Unit |
|---|---|---|---|---|
| Quiescent Current | 20 |  | uA |  |
| Shutdown Current | 0.1 |  | uA |  |
"""


def test_shifted_ec_table_is_flagged():
    findings = scan_markdown(_SHIFTED_EC)
    shifts = [f for f in findings if f.kind == "column_shift_suspect"]
    assert len(shifts) == 1
    assert "Min 5/5" in shifts[0].detail


def test_corrected_ec_table_is_clean():
    assert scan_markdown(_CORRECT_EC) == []


def test_polluted_headers_still_detect_min_max_columns():
    findings = scan_markdown(_SHIFTED_EC_POLLUTED_HEADER)
    assert [f.kind for f in findings] == ["column_shift_suspect"]


def test_short_sparse_table_is_not_flagged():
    # under the 4-row threshold: too little signal to call a shift
    assert scan_markdown(_SHORT_SPARSE) == []
