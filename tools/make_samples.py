"""Generate sample electrical invoices (digital PDF + scanned-style PNG) for testing.

Usage: python tools/make_samples.py [output_dir]
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ITEMS = [
    ("QO120", "SQUARE D QO 1P 20A PLUG-ON BREAKER", 10, "EA", 8.45, 1),
    ("THHN12SOLBLK500", "12 THHN SOL CU BLK 500FT", 2500, "FT", 152.30, 1000),
    ("52171-RE", "4SQ 2-1/8 DEEP BOX 1/2-3/4 KO", 40, "EA", 245.00, 100),
    ("HOM2448L125PGC", "HOMELINE LOAD CENTER 24SP 125A", 1, "EA", 211.37, 1),
    ("EMT34", "3/4 EMT CONDUIT 10FT", 300, "FT", 98.50, 100),
    ("221-413", "WAGO LEVER NUT 3-COND 100PK", 3, "BX", 33.15, 1),
]
PER = {1: "E", 100: "C", 1000: "M"}


def rows(price_bump: float = 0.0):
    out = []
    for n, (item, desc, qty, uom, price, per) in enumerate(ITEMS, start=1):
        p = round(price * (1 + price_bump), 2)
        out.append((n, qty, item, desc, uom, p, PER[per], round(qty * p / per, 2)))
    return out


def make_pdf(path: Path, number: str, date: str, bump: float) -> None:
    from PySide6.QtGui import QGuiApplication, QPageSize, QPdfWriter, QTextDocument

    QGuiApplication.instance() or QGuiApplication([])
    lines = rows(bump)
    subtotal = round(sum(r[-1] for r in lines), 2)
    tax = round(subtotal * 0.07, 2)
    body = "".join(
        f"<tr><td>{n}</td><td>{q}</td><td>{q}</td><td>{item}</td><td>{desc}</td><td>{uom}</td>"
        f"<td align=right>{p:,.3f}</td><td>{per}</td><td align=right>{ext:,.2f}</td></tr>"
        for n, q, item, desc, uom, p, per, ext in lines)
    html = f"""
    <h2>GRAYBAR ELECTRIC COMPANY, INC.</h2>
    <p>1200 Industrial Pkwy, Columbus OH 43215</p>
    <h3>INVOICE</h3>
    <p>Invoice No: {number} &nbsp;&nbsp; Invoice Date: {date}</p>
    <p>Bill To: Midstate Electric &nbsp;&nbsp; Customer PO: JOB-4471</p>
    <table cellpadding=3>
    <tr><th>Ln</th><th>Ord</th><th>Shp</th><th>Catalog #</th><th>Description</th><th>UOM</th><th>Price</th>
    <th>Per</th><th>Extension</th></tr>{body}</table>
    <p>Subtotal {subtotal:,.2f}</p><p>Sales Tax {tax:,.2f}</p><p>Invoice Total ${subtotal + tax:,.2f}</p>
    """
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.Letter))
    writer.setResolution(300)
    doc = QTextDocument()
    doc.setHtml(html)
    doc.print_(writer)


def make_scan(path: Path, number: str, date: str, bump: float) -> None:
    lines = rows(bump)
    img = Image.new("RGB", (2550, 3300), "white")
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype("arialbd.ttf", 56)
    font = ImageFont.truetype("arial.ttf", 34)
    d.text((150, 150), "REXEL USA", font=big, fill="black")
    d.text((150, 230), "455 Commerce Dr, Dayton OH 45402", font=font, fill="black")
    d.text((150, 330), f"INVOICE NUMBER: {number}", font=font, fill="black")
    d.text((1400, 330), f"INVOICE DATE: {date}", font=font, fill="black")
    d.text((150, 390), "SOLD TO: Midstate Electric", font=font, fill="black")
    cols = [150, 230, 380, 760, 1700, 1800, 2030, 2150]
    heads = ["LN", "QTY", "ITEM", "DESCRIPTION", "UOM", "PRICE", "", "AMOUNT"]
    y = 520
    for x, h in zip(cols, heads):
        d.text((x, y), h, font=font, fill="black")
    y += 70
    subtotal = 0.0
    for n, q, item, desc, uom, p, per, ext in lines:
        vals = [str(n), f"{q:,}", item, desc[:40], uom, f"{p:,.2f}", per, f"{ext:,.2f}"]
        for x, v in zip(cols, vals):
            d.text((x, y), v, font=font, fill="black")
        subtotal += ext
        y += 60
    d.text((1700, y + 60), f"SUBTOTAL {subtotal:,.2f}", font=font, fill="black")
    d.text((1700, y + 120), f"TOTAL ${subtotal * 1.07:,.2f}", font=font, fill="black")
    img = img.rotate(random.uniform(-0.6, 0.6), fillcolor="white", resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(path)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    make_pdf(out / "graybar_7012345678.pdf", "7012345678", "09/15/2026", 0.0)
    make_pdf(out / "graybar_7012399001.pdf", "7012399001", "10/28/2026", 0.06)
    make_scan(out / "rexel_scan_A889123.png", "A-889123", "11/02/2026", 0.03)
    print(f"Samples written to {out.resolve()}")


if __name__ == "__main__":
    main()
