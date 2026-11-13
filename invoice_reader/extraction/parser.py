"""Heuristic parser that turns raw invoice text into structured data.

The parser is tuned for electrical distributor invoices: a line item row ends
with a numeric tail (quantity, unit price, optional C/M pricing basis, and the
extended amount). Rows whose arithmetic checks out (qty * price / per ==
extended) are marked high-confidence; everything else is flagged for review.
"""
from __future__ import annotations

import re
from datetime import date
from itertools import chain

from invoice_reader.models import LineItem, ParsedInvoice, emt_item_number

KNOWN_VENDORS = [
    "Graybar", "Consolidated Electrical Distributors", "CED", "Rexel", "Border States",
    "WESCO", "Anixter", "City Electric Supply", "Crescent Electric", "Mayer Electric",
    "Platt Electric", "Elliott Electric", "Kirby Risk", "Van Meter", "State Electric",
    "Codale", "Summit Electric", "North Coast Electric", "Grainger", "US Electrical Services",
    "Facility Solutions Group", "Sonepar", "Turtle & Hughes", "Schaedler Yesco", "Dakota Supply",
    "Butler Supply", "Werner Electric", "Viking Electric", "Wholesale Electric", "Irby",
    "Gexpro", "Home Depot", "Lowe's", "Menards", "Cooper Electric", "Mid-Island Electrical",
    "Steiner Electric", "Fromm Electric", "Kendall Electric", "Shepherd Electric",
    "Independent Electric Supply", "Hite", "Revere Electric", "Loeb Electric", "Springfield Electric",
    "Ward Electric", "QED", "Feldman Lumber", "Granite City Electric", "Billows Electric",
    "Graybar Electric", "Electrical Wholesalers", "Branch Group",
    "Rexel USA", "Platt", "Mayer", "Crescent",
]
# Letterhead text -> vendor, for vendors whose full name only appears in a logo image.
BUILTIN_ALIASES = {
    "EW -": "Electrical Wholesalers",  # "EW - FRANKLIN" branch header
}

# Per-pricing markers that may follow a unit price. Per-foot ("10.820/ft") prices
# divide by 1 because the quantity is already in feet.
PER_MARKERS = {"E": 1, "EA": 1, "/E": 1, "/EA": 1, "C": 100, "/C": 100, "M": 1000, "/M": 1000,
               "FT": 1, "/FT": 1, "LF": 1, "/LF": 1}
UOM_TOKENS = {
    "EA", "E", "FT", "LF", "RL", "ROLL", "REEL", "RLS", "BX", "BOX", "PK", "PKG", "PKT", "CS",
    "CASE", "PR", "PAIR", "SET", "LOT", "KIT", "SPL", "SPOOL", "CTN", "BG", "BAG", "DZ", "IN",
    "LB", "GAL", "C", "M", "PC", "PCS", "EACH", "STK", "STICK", "BDL", "BNDL", "JOB",
}
SKIP_KEYWORDS = re.compile(
    r"\b(sub\s*-?total|total|tax|freight|shipping|handling|balance|amount\s+due|remit|"
    r"payment|terms|discount\s+if|page\s+\d|invoice\s+date|due\s+date|ship\s+to|bill\s+to|"
    r"sold\s+to|p\.?o\.?\s*(no|number|#)|order\s+(no|number|#|date)|thank\s+you|restocking|"
    r"card\s+ending|fuel\s+surcharge)\b",
    re.I,
)
NUM_RE = re.compile(r"^\(?-?\$?-?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?-?\)?$")
PRICE_UOM_RE = re.compile(r"^(\$?[\d,]*\.?\d+)/?(EA|E|C|M|FT|LF)$", re.I)
ITEM_PREFIX_RE = re.compile(r"^(CAT|ITEM|PART|PN|SKU|MFR)\s*[#:.]+", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"),
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I),
    re.compile(r"\b(\d{1,2})[\s\-](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-,]+(\d{2,4})\b", re.I),
]
INVOICE_NO_RE = re.compile(
    r"\binv(?:oice)?\.?\s*(?:no\.?|number|num\.?|#)\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
INVOICE_NO_LABEL_RE = re.compile(r"\binv(?:oice)?\.?\s*(?:no\.?|number|num\.?|#)", re.I)
INVOICE_DATE_LABEL_RE = re.compile(r"\b(invoice|inv\.?|billing|ship|order)\s*date\b", re.I)
TOTAL_RE = re.compile(
    r"\b(invoice\s+total|total\s+due|amount\s+due|balance\s+due|total\s+amount|grand\s+total|total)\b", re.I)
SUBTOTAL_RE = re.compile(r"\b(sub\s*-?total|merchandise(\s+total)?|net\s+sales)\b", re.I)


def parse_number(token: str) -> float | None:
    t = token.strip()
    if not t or not NUM_RE.match(t) or not any(c.isdigit() for c in t):
        return None
    negative = t.startswith("(") and t.endswith(")") or t.endswith("-") or t.lstrip("($").startswith("-")
    cleaned = t.strip("()$-").replace("$", "").replace(",", "").lstrip("-")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def parse_date(text: str) -> str | None:
    for i, pattern in enumerate(DATE_PATTERNS):
        for m in pattern.finditer(text):
            try:
                if i == 0:
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif i == 1:
                    mo, d, y = int(m[1]), int(m[2]), int(m[3])
                elif i == 2:
                    mo, d, y = MONTHS[m[1].lower()[:3]], int(m[2]), int(m[3])
                else:
                    d, mo, y = int(m[1]), MONTHS[m[2].lower()[:3]], int(m[3])
                if y < 100:
                    y += 2000
                if not 1990 <= y <= 2100:
                    continue
                return date(y, mo, d).isoformat()
            except (ValueError, KeyError):
                continue
    return None


QTY_UOM_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?)([A-Za-z]{1,5})\.?$")


def _split_tokens(line: str) -> list[str]:
    tokens: list[str] = []
    for tok in line.split():
        m = PRICE_UOM_RE.match(tok)
        q = QTY_UOM_RE.match(tok)
        # "1ea" / "250FT" quantity+UOM only in the leading quantity columns (e.g. "1ea 1ea" ordered/shipped),
        # so description specs such as "8FT" or "250W" are left alone.
        at_start = all(t.isdigit() or t in UOM_TOKENS or parse_number(t) is not None for t in tokens)
        if m and any(c.isdigit() for c in m[1]) and "." in m[1]:
            tokens.extend([m[1], m[2].upper()])
        elif q and at_start and q[2].upper() in UOM_TOKENS:
            tokens.extend([q[1], q[2].upper()])
        else:
            tokens.append(tok)
    return tokens


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.02, abs(b) * 0.005)


def _looks_like_item(tok: str) -> bool:
    t = ITEM_PREFIX_RE.sub("", tok)
    if len(t) < 3 or not re.match(r"^[A-Z0-9][A-Z0-9\-./#_]*$", t, re.I):
        return False
    if not any(c.isdigit() for c in t):
        return False
    if parse_number(t) is not None:
        return len(t) >= 5 and "." not in t  # UPCs / numeric catalog numbers
    return True


# Words that start descriptions and must never be mistaken for a manufacturer line code.
DESC_WORDS = {
    "BOX", "LED", "WIRE", "CABLE", "PIPE", "TAPE", "LAMP", "BULB", "CORD", "PLUG", "NUT", "NUTS", "CLIP",
    "STRAP", "BAR", "RING", "CAP", "COVER", "PLATE", "EXIT", "SIGN", "TRACK", "PANEL", "LIGHT", "HEAD",
    "UNIT", "THE", "AND", "FOR", "WITH", "NEW", "GFCI", "AFCI", "EMT", "PVC", "MC", "NM", "THHN", "SO",
    "ROMEX", "SER", "XHHW", "RGS", "IMC", "ENT", "FLEX", "CU", "AL", "BLK", "WHT", "RED", "GRN", "BLU",
    "DEEP", "SQ", "LB", "LT", "FIXT", "MINI", "BASIC", "SINGLE", "DUAL", "ROUND", "SQUARE", "DIM",
    "WHITE", "BLACK", "BROWN", "IVORY", "GRAY", "GREY", "SWITCH", "OUTLET", "DIMMER", "TOGGLE", "DUPLEX",
    "COPPER", "STEEL", "FITTING", "BUSHING", "SCREW", "ANCHOR", "BRACKET", "HANGER", "CLAMP", "LUG",
    "FUSE", "METER", "SOCKET", "BASE", "KIT", "SET", "PACK", "ROLL", "REEL", "SPOOL", "GROUND", "ROD",
}


def _looks_like_catalog(tok: str) -> bool:
    return (len(tok) >= 3 and bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-./#_+]*", tok))
            and (any(c.isdigit() for c in tok) or len(tok) >= 4) and tok not in DESC_WORDS)


# Wire / conduit types that distributors use in place of a catalog # ("WAL SER 4/0-4/0-4/0-2/0 ALUM",
# "PVC SCH 80 2\" X 10' PIPE"). The item # becomes the type plus its size tokens so sizes stay distinct.
COMMODITY_TYPES = {
    "SER", "SEU", "URD", "USE", "RX", "NM", "NMB", "UF", "THHN", "THWN", "XHHW", "MC", "MCAP", "AC", "SO",
    "SJ", "SJOOW", "SOOW", "TSTAT", "TFFN", "SCH", "EMT", "RGS", "IMC", "ENT", "FMC", "LFMC", "LFNC", "CAT5E",
    "CAT6", "CAT6A", "RG6", "RG59", "COAX", "BARE", "SD", "SOL", "STR", "TC", "FPLP", "FPL", "ALARM",
}
_MFR_RE = re.compile(r"[A-Z][A-Z&]{1,6}")


def _commodity_item(tokens: list[str]) -> int:
    """Number of tokens forming a commodity item # at the start of `tokens` (0 if not one)."""
    if len(tokens) < 2 or tokens[0] not in COMMODITY_TYPES or not any(c.isdigit() for c in tokens[1]):
        return 0
    n = 1
    while n < len(tokens) and n <= 3 and any(c.isdigit() for c in tokens[n]):
        n += 1
    return n


def parse_line_item(line: str) -> LineItem | None:
    if SKIP_KEYWORDS.search(line):
        return None
    tokens = _split_tokens(line)
    if len(tokens) < 3:
        return None

    nums = [(i, v) for i, t in enumerate(tokens) if (v := parse_number(t)) is not None]
    if len(nums) < 2:
        return None

    ext_i, ext_v = nums[-1]
    trailing = tokens[ext_i + 1:]
    if len(trailing) > 1 or (trailing and (len(trailing[0]) > 2 or any(c.isdigit() for c in trailing[0]))):
        return None
    if "." not in tokens[ext_i]:
        return None

    def per_for(idx: int) -> int | None:
        nxt = tokens[idx + 1].upper() if idx + 1 < len(tokens) else ""
        return PER_MARKERS.get(nxt)

    before_ext = [(i, v) for i, v in nums if i < ext_i]
    match = None
    for p_i, p_v in reversed(before_ext[-3:]):
        if p_v <= 0:
            continue
        explicit = per_for(p_i)
        pers = [explicit] if explicit else [1, 100, 1000]
        qty_candidates = [(i, v) for i, v in reversed(before_ext) if i != p_i and v != 0]
        for per in pers:
            for q_i, q_v in qty_candidates:
                if _close(abs(q_v) * p_v / per, abs(ext_v)):
                    match = (q_i, q_v, p_i, p_v, per, explicit is not None)
                    break
            if match:
                break
        if match:
            break

    confidence = "high"
    if match:
        q_i, qty, p_i, price, per, _ = match
    else:
        # Fallback: last decimal before the extended amount is the price.
        decimals = [(i, v) for i, v in before_ext if "." in tokens[i] and v > 0]
        if not decimals:
            return None
        p_i, price = decimals[-1]
        per = per_for(p_i) or 1
        ints = [(i, v) for i, v in before_ext if i < p_i and v > 0]
        if ints:
            q_i, qty = ints[-1]
        else:
            q_i, qty = -1, round(ext_v / (price / per), 3) if price else 1
        confidence = "low"

    used = {ext_i, p_i, q_i}
    used.update(i for i in range(ext_i + 1, len(tokens)))
    if per_for(p_i):
        used.add(p_i + 1)
    uom = "EA"
    if q_i >= 0 and q_i + 1 < len(tokens) and tokens[q_i + 1].upper() in UOM_TOKENS and q_i + 1 != p_i:
        uom = tokens[q_i + 1].upper().rstrip(".")
        used.add(q_i + 1)
    elif p_i - 1 > max(q_i, 0) and tokens[p_i - 1].upper() in UOM_TOKENS:
        # Layout "... DESCRIPTION UOM PRICE PER EXTENSION"
        uom = tokens[p_i - 1].upper()
        used.add(p_i - 1)
    if uom == "E":
        uom = "EA"
    # Anything between the unit price and extended (discount %, list price) is not description.
    used.update(range(p_i + 1, ext_i))

    remaining = [(i, t) for i, t in enumerate(tokens) if i not in used and i < p_i]
    # Drop leading line numbers / ordered / backordered quantities (and their UOMs), but keep
    # numeric catalog numbers (e.g. "2412") that follow them.
    after_number = False
    while remaining:
        idx, tok = remaining[0]
        if after_number and tok.upper() in UOM_TOKENS:
            remaining.pop(0)
            after_number = False
            continue
        val = parse_number(tok)
        if val is None or "." in tok:
            break
        if (idx == 0 and val < 1000) or val == qty or val == 0:
            remaining.pop(0)
            after_number = True
            continue
        break
    if not remaining:
        return None

    first = remaining[0][1]
    words = [t for _, t in remaining]
    manufacturer = ""
    mfr_idx = -1
    item_span = 1
    bare_emt = emt_item_number("EMT", " ".join(words))
    if bare_emt:
        item_idx, item_span = 0, len(words)
    elif first.isdigit() and len(first) >= 3:
        item_idx = 0
    elif len(words) >= 3 and _MFR_RE.fullmatch(first) and (n := _commodity_item(words[1:])):
        # "WAL SER 4/0-4/0-4/0-2/0 ALUM" -> mfr WAL, item "SER 4/0-4/0-4/0-2/0"
        manufacturer, mfr_idx, item_idx, item_span = first, 0, 1, n
    elif first == "EMT" and (n := _commodity_item(words)):
        item_idx, item_span = 0, n
    elif (len(words) >= 2 and _MFR_RE.fullmatch(first) and first not in DESC_WORDS
          and (_looks_like_catalog(words[1]) or (words[1].isdigit() and len(words) >= 3))):
        # Distributor layout "MFR CATALOG# DESCRIPTION", e.g. "RAB EZPAN2X2-30N/D10 EDGELIT PANEL", "CRS 11 1/2\"".
        manufacturer, mfr_idx, item_idx = first, 0, 1
    else:
        item_idx = next((k for k, (_, t) in enumerate(remaining) if _looks_like_item(t)), None)
    if item_idx is None:
        if confidence == "low":
            return None
        item_idx = 0
    item_ks = set(range(item_idx, item_idx + item_span))
    item_number = " ".join(ITEM_PREFIX_RE.sub("", words[k]) if k == item_idx else words[k]
                           for k in sorted(item_ks)).strip(".,;:")
    desc_tokens = [t for k, t in enumerate(words)
                   if (k not in item_ks or item_span > 1) and k != mfr_idx]
    description = " ".join(desc_tokens).strip(" -|")
    item_number = bare_emt or emt_item_number(item_number, description) or item_number
    if not item_number:
        return None

    return LineItem(
        item_number=item_number.upper(),
        description=description,
        quantity=abs(qty),
        uom=uom,
        unit_price=round(price, 5),
        price_per=per,
        extended=round(ext_v, 2),
        confidence=confidence,
        raw=line.strip(),
        manufacturer=manufacturer,
    )


ORDER_NO_RE = re.compile(
    r"\b(?:sales\s+)?order\.?\s*(?:no\.?|number|num\.?|#)\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
ORDER_NO_LABEL_RE = re.compile(r"\b(?:sales\s+)?order\.?\s*(?:no\.?|number|num\.?|#)", re.I)


def _find_doc_number(lines: list[str], value_re: re.Pattern, label_re: re.Pattern) -> str:
    for idx, line in enumerate(lines[:60]):
        m = value_re.search(line)
        if m and any(c.isdigit() for c in m[1]) and not parse_date(m[1]):
            return m[1].strip(".-/")
        if label_re.search(line) and idx + 1 < len(lines):
            # Tabular header: labels on one line, values on the next.
            label_pos = len(label_re.split(line)[0].split())
            values = lines[idx + 1].split()
            ordered = values[label_pos:] + values[:label_pos]
            for tok in ordered:
                if any(c.isdigit() for c in tok) and len(tok) >= 4 and not parse_date(tok):
                    return tok.strip(".-/#:")
    return ""


def _find_invoice_number(lines: list[str]) -> str:
    # Order acknowledgements carry only an order number; use it when there's no invoice number.
    return (_find_doc_number(lines, INVOICE_NO_RE, INVOICE_NO_LABEL_RE)
            or _find_doc_number(lines, ORDER_NO_RE, ORDER_NO_LABEL_RE))


def _find_date(lines: list[str]) -> str | None:
    for idx, line in enumerate(lines[:60]):
        if INVOICE_DATE_LABEL_RE.search(line):
            after = INVOICE_DATE_LABEL_RE.split(line, maxsplit=1)[-1]
            if d := parse_date(after) or parse_date(line):
                return d
            if idx + 1 < len(lines) and (d := parse_date(lines[idx + 1])):
                return d
    for line in lines[:40]:
        if d := parse_date(line):
            return d
    return None


def _find_amount(lines: list[str], pattern: re.Pattern, exclude: re.Pattern | None = None) -> float | None:
    found = None
    for line in lines:
        if pattern.search(line) and not (exclude and exclude.search(line)):
            nums = [v for t in line.split() if (v := parse_number(t)) is not None and "." in t]
            if nums:
                found = nums[-1]
    return found


def find_known_vendor(text: str, known_vendors: list[str], exclude: list[str],
                      aliases: dict[str, str] | None = None) -> str:
    """Find a known vendor name (or learned alias) in text. Earliest match wins (letterhead)."""
    excluded = [e.lower() for e in exclude if e.strip()]
    names = {c: c for c in chain(known_vendors, KNOWN_VENDORS)}
    names.update(BUILTIN_ALIASES)
    names.update({a: v for a, v in (aliases or {}).items()})
    candidates = {k: v for k, v in names.items()
                  if k.strip() and not any(k.lower() in e or e in k.lower() for e in excluded)}
    lines = text.splitlines()
    for scope in ("\n".join(lines[:40]).lower(), text.lower()):
        best: tuple[int, int, str] | None = None
        for key, vendor in candidates.items():
            m = re.search(r"(?<![a-z0-9])" + re.escape(key.lower()) + r"(?![a-z0-9])", scope)
            if m:
                rank = (m.start(), -len(key), vendor)
                if best is None or rank < best:
                    best = rank
        if best:
            return best[2]
    return ""


TITLE_WORDS_RE = re.compile(
    r"invoice|page|date|acknowledg|order|statement|packing|quot|credit|memo|sold\s+to|ship\s+to|bill\s+to|"
    r"remit|receipt|customer|subtotal|total", re.I)


def _find_vendor(lines: list[str], known_vendors: list[str], exclude: list[str],
                 aliases: dict[str, str]) -> tuple[str, bool]:
    """Returns (vendor, confident). Not confident = best guess from the letterhead text."""
    found = find_known_vendor("\n".join(lines), known_vendors, exclude, aliases)
    if found:
        return found, True
    excluded = [e.lower() for e in exclude if e.strip()]
    for line in lines[:12]:
        clean = line.strip()
        letters = sum(c.isalpha() for c in clean)
        if (letters >= 4 and letters > len(clean) * 0.6 and not TITLE_WORDS_RE.search(clean)
                and not any(e in clean.lower() for e in excluded)):
            guess = clean.title()[:80]
            return aliases.get(guess, guess), guess in aliases
    return "", False


def _normalize_line(line: str) -> str:
    line = re.sub(r"[ \t]+", " ", line).strip()
    line = re.sub(r"(\d) ,\s?(\d{3})(?=[.,\s]|$)", r"\1,\2", line)  # OCR: "1 ,288.92" -> "1,288.92"
    line = re.sub(r"(\d), (\d{3}\.\d{2})\b", r"\1,\2", line)
    line = re.sub(r"\$ (?=\d)", "$", line)
    return line


CONT_HEADER_RE = re.compile(r"\b(description|unit\s+price|ext(ended)?\s+price|order\s+qty|qty|amount)\b", re.I)


def _is_continuation(line: str) -> bool:
    """A wrapped description line belonging to the preceding item."""
    if line.startswith("*") or SKIP_KEYWORDS.search(line) or CONT_HEADER_RE.search(line):
        return False
    if len(line) > 70 or sum(c.isalpha() for c in line) < 2:
        return False
    if sum(c.islower() for c in line) > 2:  # boilerplate prose, not item descriptions
        return False
    return not re.search(r"\d\.\d{2}\s*$", line)


def parse_invoice_text(text: str, known_vendors: list[str] | None = None,
                       exclude_names: list[str] | None = None,
                       vendor_aliases: dict[str, str] | None = None) -> ParsedInvoice:
    """exclude_names: our own company names, so the bill-to isn't taken as vendor.
    vendor_aliases: learned {letterhead text: vendor name} corrections."""
    lines = [_normalize_line(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    inv = ParsedInvoice(raw_text=text)
    inv.vendor, inv.vendor_confident = _find_vendor(lines, known_vendors or [], exclude_names or [],
                                                    vendor_aliases or {})
    inv.invoice_number = _find_invoice_number(lines)
    inv.invoice_date = _find_date(lines)
    inv.subtotal = _find_amount(lines, SUBTOTAL_RE)
    inv.total = _find_amount(lines, TOTAL_RE, exclude=SUBTOTAL_RE)

    last: LineItem | None = None
    wrapped = 0
    for line in lines:
        item = parse_line_item(line)
        if item:
            inv.lines.append(item)
            last, wrapped = item, 0
        elif last is not None and wrapped < 5 and _is_continuation(line):
            last.description = f"{last.description} {line}".strip()[:250]
            wrapped += 1
        else:
            last = None

    finalize_warnings(inv)
    return inv


def finalize_warnings(inv: ParsedInvoice) -> None:
    inv.warnings = []
    if not inv.lines:
        inv.warnings.append("No line items were recognized.")
    if not inv.invoice_number:
        inv.warnings.append("Invoice number not found.")
    if not inv.invoice_date:
        inv.warnings.append("Invoice date not found.")
    if not inv.vendor:
        inv.warnings.append("Vendor not recognized.")
    elif not inv.vendor_confident:
        inv.warnings.append(f"Vendor \"{inv.vendor}\" was guessed from the letterhead - please confirm.")
    low = sum(1 for l in inv.lines if l.confidence != "high")
    if low:
        inv.warnings.append(f"{low} line(s) could not be verified (qty x price != extended).")
    if inv.lines:
        reference = inv.subtotal
        if reference is not None and not _close(inv.lines_total, reference):
            inv.warnings.append(
                f"Line items total ${inv.lines_total:,.2f} does not match subtotal ${reference:,.2f}.")
        elif reference is None and inv.total is not None and inv.lines_total > inv.total + 0.01:
            inv.warnings.append(
                f"Line items total ${inv.lines_total:,.2f} exceeds invoice total ${inv.total:,.2f}.")
