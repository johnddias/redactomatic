"""First-page-text classifier that selects which vendor extractor to run.

Each brokerage lays out its statements differently, so table extraction
logic doesn't generalize across vendors. This looks for identifying
marker strings on the document's first page and returns a document-type
key that the package's dispatcher uses to pick an extractor module. An
unrecognized document returns "unknown" so callers can fall back to
redaction-only, with no table extraction attempted.
"""

import pdfplumber

# Marker substrings (matched against first-page text) mapped to a
# document-type key. Add an entry here plus a matching extractor module
# to support a new vendor.
_MARKERS = {
    "jpmc": ("J.P. Morgan Securities", "JPMS LLC"),
    "chase": ("JPMorgan Chase Bank",),
}

UNKNOWN = "unknown"


def classify(pdf_path: str) -> str:
    """Return a document-type key (e.g. "jpmc") or UNKNOWN."""
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return UNKNOWN
        first_page_text = pdf.pages[0].extract_text() or ""

    for doc_type, markers in _MARKERS.items():
        if any(marker in first_page_text for marker in markers):
            return doc_type
    return UNKNOWN
