"""
PDF redaction engine.

Searches each page of a PDF for PII terms loaded from a control file and
replaces every match with a solid black rectangle.  Metadata is also stripped.
Output is written to a new file with a .red.pdf extension.
"""

import pathlib
import fitz  # PyMuPDF


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
        for variant in _case_variants(term):
            hits = page.search_for(variant, quads=False)
            for rect in hits:
                page.add_redact_annot(rect, fill=(0, 0, 0))
                added += 1
    return added


def redact_pdf(input_path: str, control_path: str) -> str:
    """Redact *input_path* using PII terms from *control_path*.

    Returns the path of the newly created redacted file (*.red.pdf*).
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

    doc: fitz.Document = fitz.open(input_path)

    total_redactions = 0
    for page in doc:
        n = _redact_page(page, terms)
        if n:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        total_redactions += n

    # Strip all metadata
    doc.set_metadata({})

    # Remove any XMP metadata stream
    doc.del_xml_metadata()

    doc.save(str(out_path), garbage=4, deflate=True, clean=True)
    doc.close()

    return str(out_path), total_redactions
