import pytest

from invoice_reader.extraction.parser import parse_date, parse_invoice_text, parse_line_item, parse_number

SAMPLE = """\
GRAYBAR ELECTRIC COMPANY, INC.
123 Industrial Pkwy, Columbus OH 43215
INVOICE
Invoice No: 7012345678   Invoice Date: 10/28/2026
Customer PO: JOB-4471   Bill To: Midstate Electric
Ln Qty Ord Qty Shp Catalog # Description Price UOM Extension
1 10 10 QO120 SQUARE D QO 1P 20A PLUG-ON BREAKER 8.450 E 84.50
2 2500 2500 THHN12SOLBLK500 12 THHN SOL CU BLK 500FT 152.300 M 380.75
3 4 4 2412 2-1/8 DEEP 4SQ BOX 1/2 & 3/4 KO 245.00 C 9.80
4 1 1 HOM2448L125PGC HOMELINE LOAD CENTER 24SP 125A 211.37 211.37
5 3 3 78371230 WAGO 221-413 LEVER NUTS 100PK 33.15 EA 99.45
Subtotal 785.87
Sales Tax 55.01
Invoice Total $840.88
"""


def test_parse_number_variants():
    assert parse_number("1,234.50") == 1234.5
    assert parse_number("$84.50") == 84.5
    assert parse_number("(12.00)") == -12.0
    assert parse_number("12.00-") == -12.0
    assert parse_number("QO120") is None
    assert parse_number("4/0") is None


@pytest.mark.parametrize("text,expected", [
    ("Invoice Date: 10/28/2026", "2026-10-28"),
    ("2026-01-05", "2026-01-05"),
    ("Nov 3, 2026", "2026-11-03"),
    ("03-Nov-26", "2026-11-03"),
    ("no date here", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_full_invoice():
    inv = parse_invoice_text(SAMPLE, exclude_names=["Midstate Electric"])
    assert inv.vendor.lower().startswith("graybar")
    assert inv.invoice_number == "7012345678"
    assert inv.invoice_date == "2026-10-28"
    assert inv.subtotal == 785.87
    assert inv.total == 840.88
    assert len(inv.lines) == 5, [l.raw for l in inv.lines]
    by_item = {l.item_number: l for l in inv.lines}

    qo = by_item["QO120"]
    assert qo.quantity == 10 and qo.unit_price == 8.45 and qo.price_per == 1
    assert "BREAKER" in qo.description and qo.confidence == "high"

    wire = by_item["THHN12SOLBLK500"]
    assert wire.price_per == 1000 and wire.quantity == 2500
    assert wire.unit_cost == pytest.approx(0.1523)

    box = by_item["2412"] if "2412" in by_item else None
    assert box is not None and box.price_per == 100

    lc = by_item["HOM2448L125PGC"]
    assert lc.quantity == 1 and lc.unit_price == 211.37

    wago = by_item["78371230"]
    assert wago.quantity == 3 and wago.uom == "EA"
    assert all(l.confidence == "high" for l in inv.lines)
    assert inv.is_clean, inv.warnings


def test_skips_totals_and_headers():
    assert parse_line_item("Invoice Total $840.88") is None
    assert parse_line_item("Sales Tax 55.01") is None
    assert parse_line_item("Page 1 of 2") is None


def test_combined_price_uom_token():
    item = parse_line_item("500 FT MC122 12/2 MC CABLE ALUM 52.30/C 261.50")
    assert item is not None
    assert item.price_per == 100 and item.quantity == 500 and item.uom == "FT"
    assert item.item_number == "MC122"


def test_price_per_foot_marker():
    item = parse_line_item("125ft 125ft CBLMSTR CM-HSBCC-LRGPLUS2-JT84 CABLE 10.820/ft 1352.50")
    assert item is not None
    assert (item.quantity, item.uom, item.unit_price, item.price_per, item.extended) == (
        125, "FT", 10.82, 1, 1352.50)
    assert item.confidence == "high"
    assert item.manufacturer == "CBLMSTR" and item.item_number == "CM-HSBCC-LRGPLUS2-JT84"


def test_unverifiable_line_is_low_confidence():
    item = parse_line_item("1 2 EMT34 3/4 EMT CONDUIT 10FT 7.25 99.99")
    assert item is not None and item.confidence == "low"


def test_tabular_header_values_on_next_line():
    text = "Rexel\nInvoice Number Invoice Date\nA-889123 11/02/2026\n1 5 PVC2 2IN SCH40 PVC 3.10 15.50\n"
    inv = parse_invoice_text(text)
    assert inv.vendor == "Rexel"
    assert inv.invoice_number == "A-889123"
    assert inv.invoice_date == "2026-11-02"
    assert len(inv.lines) == 1


@pytest.mark.parametrize("catalog,description,expected", [
    ("EMT2", '1" X 10', "EMT1"),
    ("EMT2", "1inch x10 emt", "EMT1"),
    ("EMT2", '2" X 10FT EMT', "EMT2"),
    ("EMT", '1" X 10\' PIPE', "EMT1"),
    ("EMT", '1/2" X 10FT', 'EMT 1/2"'),
    ("EMT", '1-1/2" X 10FT', 'EMT 3/2"'),
    ("EMT", '1.5" X 10FT', 'EMT 3/2"'),
    ("", "1inch x10 emt", "EMT1"),
    ("EMT150", '1-1/2" NM TERMINATOR', "EMT150"),
])
def test_emt_diameter_matching(catalog, description, expected):
    line = parse_line_item(f"40 FT {catalog} {description} 202.64/C 81.06")
    assert line is not None
    assert line.item_number == expected
    assert line.description == (f"EMT {description}" if catalog == "EMT" else description)
    assert (line.quantity, line.uom, line.unit_price, line.price_per, line.extended) == (
        40, "FT", 202.64, 100, 81.06)
    assert line.confidence == "high"
