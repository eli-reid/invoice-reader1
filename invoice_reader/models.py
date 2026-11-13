from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from fractions import Fraction

# Electrical distributors price commodity items (wire, conduit, fittings) per
# hundred (C) or per thousand (M). Unit prices are stored as quoted, together
# with the basis, so per-each cost can always be derived.
PRICE_PER_SUFFIX = {1: "", 100: "/C", 1000: "/M"}
PRICE_PER_LABEL = {1: "Each (E)", 100: "Per 100 (C)", 1000: "Per 1000 (M)"}

EMT_SIZE_RE = re.compile(
    r'^(?:EMT\s+)?(?P<size>\d+(?:[- ]\d+/\d+|/\d+|\.\d+)?)\s*'
    r'(?:"|IN(?:CH(?:ES)?)?\.?)\s*X\s*10\s*(?:FT\.?|FEET|\'|FOOT)?'
    r'(?:\s*(?:EMT|CONDUIT|PIPE))*$', re.I)


def emt_item_number(item_number: str, description: str) -> str | None:
    """Correct generic EMT catalog labels only when a 10-foot conduit size is explicit."""
    if not re.fullmatch(r'EMT(?:\d+|\s+\d+(?:[- ]\d+/\d+|/\d+|\.\d+)?")?', item_number.strip(), re.I):
        return None
    text = description.strip().replace("\u201d", '"').replace("\u2033", '"')
    match = EMT_SIZE_RE.fullmatch(text)
    if match is None:
        return None
    parts = match["size"].replace("-", " ").split()
    try:
        size = sum((Fraction(p) for p in parts), Fraction(0))
    except (ValueError, ZeroDivisionError):
        return None
    if size <= 0:
        return None
    return f"EMT{size.numerator}" if size.denominator == 1 else f'EMT {size}"'


@dataclass
class LineItem:
    item_number: str = ""
    description: str = ""
    quantity: float = 0.0
    uom: str = "EA"
    unit_price: float = 0.0
    price_per: int = 1
    extended: float | None = None
    confidence: str = "high"  # "high" when qty * price == extended, else "low"
    raw: str = ""
    manufacturer: str = ""

    @property
    def unit_cost(self) -> float:
        return self.unit_price / (self.price_per or 1)

    @property
    def computed_extended(self) -> float:
        return round(self.quantity * self.unit_cost, 2)


@dataclass
class ParsedInvoice:
    source_path: str = ""
    file_hash: str = ""
    vendor: str = ""
    vendor_confident: bool = False
    invoice_number: str = ""
    invoice_date: str | None = None  # ISO yyyy-mm-dd
    total: float | None = None
    subtotal: float | None = None
    method: str = ""  # text | ocr | mixed
    lines: list[LineItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def lines_total(self) -> float:
        return round(sum((l.extended if l.extended is not None else l.computed_extended) for l in self.lines), 2)

    @property
    def looks_like_invoice(self) -> bool:
        text = self.raw_text.lower()
        return bool(self.lines) or ("invoice" in text and bool(self.invoice_number))

    @property
    def is_clean(self) -> bool:
        """True when the parse is trustworthy enough to import without review."""
        return bool(
            self.lines
            and self.vendor
            and self.invoice_number
            and self.invoice_date
            and not self.warnings
            and all(l.confidence == "high" for l in self.lines)
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "ParsedInvoice":
        data = json.loads(payload)
        lines = [LineItem(**l) for l in data.pop("lines", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(lines=lines, **{k: v for k, v in data.items() if k in known})
