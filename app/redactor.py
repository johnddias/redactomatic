"""
PDF redaction engine.

Searches each page of a PDF for PII terms loaded from a control file and
replaces every match with a solid black rectangle.  Metadata is also stripped.
Output is written to a new file with a .red.pdf extension.
"""

import pathlib
import re
import fitz  # PyMuPDF
import pdfplumber

from document_extractors import UNKNOWN, extract, write_tables_markdown, write_tables_json

# Matches standard SSN format: 123-45-6789
_SSN_RE = re.compile(r"^(\d{3})-(\d{2})-(\d{4})$")

# Matches "(Acct # NNNN)" style account references, three separate
# pdfplumber words: '(Acct', '#', '<number>)'. The number itself varies
# in format -- a bare 4-digit account reference on documents that already
# had a longer identifier redacted out of them, or a full dash-separated
# account number on an untouched original -- so this matches digits and
# dashes generically rather than assuming one shape, to make sure the
# *whole* token gets redacted.
_ACCT_NUM_RE = re.compile(r"^[\d-]+\)?$")


# ---------------------------------------------------------------------------
# Control-file parsing
# ---------------------------------------------------------------------------

def load_pii_terms(control_path: str) -> list[str]:
    """Return a deduplicated list of non-empty PII terms from *control_path*.

    Lines that start with '#' are treated as comments and ignored.
    Leading/trailing whitespace is stripped from every line.
    """
    terms: list[str] = []
    seen: set[str] = set()

    with open(control_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                terms.append(line)

    return terms


# ---------------------------------------------------------------------------
# Core redaction
# ---------------------------------------------------------------------------

def _same_row(r1: fitz.Rect, r2: fitz.Rect) -> bool:
    """True when two rects share roughly the same horizontal band."""
    h = min(r1.height, r2.height)
    return abs((r1.y0 + r1.y1) / 2 - (r2.y0 + r2.y1) / 2) <= h * 0.75


def _find_ssn_rects(page: fitz.Page, ssn: str) -> list[fitz.Rect]:
    """Return bounding rects covering each occurrence of *ssn*.

    Handles both continuous text ("123-45-6789") and tax-form discrete boxes
    where each SSN segment lives in its own text element.  Only groups of all
    three parts that appear left-to-right on the same row within a reasonable
    horizontal gap are considered matches, preventing the individual digit
    strings from triggering false positives elsewhere on the page.
    """
    m = _SSN_RE.match(ssn)
    if not m:
        return []

    part_rects = [page.search_for(m.group(i), quads=False) for i in (1, 2, 3)]
    if not all(part_rects):
        return []

    results: list[fitz.Rect] = []
    for r1 in part_rects[0]:
        for r2 in part_rects[1]:
            if r2.x0 <= r1.x1 or not _same_row(r1, r2):
                continue
            max_gap = max(r1.height, r2.height) * 5
            if r2.x0 - r1.x1 > max_gap:
                continue
            for r3 in part_rects[2]:
                if r3.x0 <= r2.x1 or not _same_row(r1, r3):
                    continue
                if r3.x0 - r2.x1 > max_gap:
                    continue
                # One bounding rect covers all three parts and any gaps between them.
                results.append(fitz.Rect(
                    r1.x0, min(r1.y0, r2.y0, r3.y0),
                    r3.x1, max(r1.y1, r2.y1, r3.y1),
                ))
    return results


def _case_variants(term: str) -> list[str]:
    """Return a small set of case variants so matching is case-insensitive."""
    variants: list[str] = []
    for v in (term, term.lower(), term.upper(), term.title()):
        if v not in variants:
            variants.append(v)
    return variants


def _redact_page(page: fitz.Page, terms: list[str]) -> int:
    """Add redaction annotations for every occurrence of every term on *page*.

    Returns the number of redaction rectangles added.
    """
    added = 0
    for term in terms:
        if _SSN_RE.match(term):
            for rect in _find_ssn_rects(page, term):
                page.add_redact_annot(rect, fill=(0, 0, 0))
                added += 1
        else:
            for variant in _case_variants(term):
                for rect in page.search_for(variant, quads=False):
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    added += 1
    return added


def _redact_account_numbers(page: fitz.Page, plumber_page) -> int:
    """Redact just the digits of "(Acct # NNNN)" references, row-scoped.

    These statements repeat "JPMS LLC IRA (Acct # NNNN) ... Statement
    Period: ..." on one line per page. Redacting the whole line (or a
    naive full-page text search for the bare account number) risks
    bleeding into unrelated data sharing that row -- e.g. a market value
    that happens to equal the account number, or the adjacent statement
    period text. pdfplumber's word grouping tells us *which* occurrence
    of the number is the account reference; we then look up that word's
    actual glyph rect via PyMuPDF's own search rather than trusting
    pdfplumber's reported bounding box, since some source PDFs (e.g. ones
    that already went through a prior redact/re-save cycle) carry
    degenerate per-glyph height metrics that make pdfplumber's box far
    too short to blank the text.
    """
    words = plumber_page.extract_words(use_text_flow=False, keep_blank_chars=False)
    added = 0
    for i in range(len(words) - 2):
        w1, w2, w3 = words[i], words[i + 1], words[i + 2]
        if w1["text"] == "(Acct" and w2["text"] == "#" and _ACCT_NUM_RE.match(w3["text"]):
            for rect in page.search_for(w3["text"]):
                if abs(rect.x0 - w3["x0"]) < 2:
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    added += 1
                    break
    return added


def redact_pdf(input_path: str, control_path: str, build_tables: bool = True) -> str:
    """Redact *input_path* using PII terms from *control_path*.

    Alongside the existing term-based redaction, runs a pdfplumber-based
    pass that redacts account-number references at row/column scope, and,
    for recognized statement vendors, extracts every holdings/transaction
    row into a markdown sidecar file (``<name>.tables.md``) -- e.g.
    correctly separated quantity/price/market-value columns despite the
    footnote-column bleed in JPMS's source layout, or Post Date/Merchant/
    Amount/Reference ID columns split out of a Chase statement's free-text
    description. For doc types whose row type carries a field the markdown
    table doesn't (currently just Holding.symbol), also writes a
    ``<name>.holdings.json`` sidecar with the full row data. Unrecognized
    document types fall back to redaction-only -- no table extraction is
    attempted and ``tables_path``/``holdings_json_path`` are both None.
    Passing ``build_tables=False`` skips table extraction entirely
    regardless of document type.

    Returns ``(out_path, total_redactions, tables_path, holdings_json_path)``.
    Raises ValueError when the control file contains no usable terms.
    """
    terms = load_pii_terms(control_path)
    if not terms:
        raise ValueError("Control file contains no redaction terms.")

    src = pathlib.Path(input_path)
    out_path = src.with_suffix("").with_suffix(".red.pdf")
    # Handle files that already end with .red to avoid .red.red.pdf
    if src.stem.endswith(".red"):
        out_path = src.with_name(src.stem + ".pdf").with_suffix(".red.pdf")

    tables_path = None
    holdings_json_path = None
    if build_tables:
        doc_type, rows = extract(input_path)
        if doc_type != UNKNOWN:
            tables_path = write_tables_markdown(str(out_path), doc_type, rows)
            holdings_json_path = write_tables_json(str(out_path), doc_type, rows)

    doc: fitz.Document = fitz.open(input_path)

    total_redactions = 0
    with pdfplumber.open(input_path) as plumber_doc:
        for page, plumber_page in zip(doc, plumber_doc.pages):
            n = _redact_page(page, terms)
            n += _redact_account_numbers(page, plumber_page)
            if n:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
            total_redactions += n

    # Strip all metadata
    doc.set_metadata({})

    # Remove any XMP metadata stream
    doc.del_xml_metadata()

    doc.save(str(out_path), garbage=4, deflate=True, clean=True)
    doc.close()

    return str(out_path), total_redactions, tables_path, holdings_json_path
