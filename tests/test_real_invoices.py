"""Regression tests against real Electrical Wholesalers documents."""
from pathlib import Path

import pytest

from invoice_reader.extraction.pipeline import process_file

FIXTURES = Path(__file__).parent / "fixtures"
EXCLUDE = ["Midstate Electric"]


def _lines(inv):
    return {l.item_number: l for l in inv.lines}


def test_ew_order_acknowledgement():
    inv = process_file(FIXTURES / "ew_S117435424.pdf", exclude_names=EXCLUDE)
    assert inv.vendor == "Electrical Wholesalers"
    assert inv.invoice_number == "S117435424"
    assert inv.invoice_date == "2021-02-12"
    assert inv.subtotal == pytest.approx(503.78)
    assert inv.total == pytest.approx(535.27)
    assert len(inv.lines) == 13
    assert inv.lines_total == pytest.approx(503.78)
    lines = _lines(inv)
    assert lines["EZPAN2X2-30N/D10"].manufacturer == "RAB"
    assert lines["EZPAN2X2-30N/D10"].unit_price == pytest.approx(49.95)
    assert "RECESSED DIM LED WH" in lines["EZPAN2X2-30N/D10"].description
    assert lines["APEL"].manufacturer == "SL"
    assert lines["6002NBK"].manufacturer == "LOL"
    assert lines["NTH-124B/L"].manufacturer == "NORA"
    assert all(l.quantity == 1 and l.uom == "EA" for l in inv.lines)
    assert inv.is_clean


def test_ew_invoice():
    inv = process_file(FIXTURES / "ew_S131634067.pdf", exclude_names=EXCLUDE)
    assert inv.vendor == "Electrical Wholesalers"
    assert inv.invoice_number == "S131634067.001"
    assert inv.invoice_date == "2026-11-03"
    assert inv.subtotal == pytest.approx(789.56)
    assert inv.total == pytest.approx(838.91)
    assert len(inv.lines) == 8
    assert inv.lines_total == pytest.approx(789.56)
    lines = _lines(inv)
    dryer = lines["86222"]
    assert (dryer.manufacturer, dryer.unit_price, dryer.price_per, dryer.extended) == ("MULB", 359.05, 100, 3.59)
    halo = lines["LT56079FS1EWHR"]
    assert (halo.manufacturer, halo.quantity, halo.unit_price) == ("HALO", 40, 13.01)
    assert lines["BK125LWH"].manufacturer == "NUTBRN"
    assert lines["PAR38FA-19-9CCT-40D-DIM"].quantity == 6
    assert inv.is_clean


def test_ew_invoice_commodity_items():
    inv = process_file(FIXTURES / "ew_S131600682.pdf", exclude_names=EXCLUDE)
    assert (inv.vendor, inv.invoice_number, inv.invoice_date) == (
        "Electrical Wholesalers", "S131600682.001", "2026-10-29")
    assert inv.subtotal == pytest.approx(1753.53)
    assert inv.total == pytest.approx(1863.13)
    assert len(inv.lines) == 24
    assert inv.lines_total == pytest.approx(1753.53)
    lines = _lines(inv)
    ser = lines["SER 4/0-4/0-4/0-2/0"]
    assert (ser.manufacturer, ser.quantity, ser.uom, ser.price_per, ser.extended) == ("WAL", 22, "FT", 1000, 121.64)
    assert lines['SCH 80 2"'].description == 'SCH 80 2" X 10\' PIPE'
    assert lines['SCH 40 1"'].manufacturer == "PVC"
    assert lines["TSTAT 18/6"].manufacturer == "CAB"
    assert lines["RX 6/3"].manufacturer == "WCU"
    assert (lines["11"].manufacturer, lines["11"].description) == ("CRS", '1/2" STEEL LOCKNUT')
    staple = lines["06024"]
    assert (staple.manufacturer, staple.quantity, staple.price_per) == ("KNGMFG", 2500, 1000)
    assert inv.is_clean
