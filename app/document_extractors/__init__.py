"""
Vendor-specific holdings-table extraction, dispatched by document type.

Each brokerage/statement vendor lays out its holdings tables differently
(column positions, footnote placement, multi-lot breakdown rows), so a
single extraction routine doesn't generalize across vendors. `extract`
classifies the document from its first page and hands off to the
matching vendor module. Unrecognized documents fall back to
redaction-only: no table extraction is attempted, and an empty holdings
list is returned so callers can skip producing a tables sidecar.

To support a new vendor: add a marker in classifier.py, add a
`<vendor>.py` module exposing `extract_holdings(pdf_path) -> list[Holding]`,
and register it in `_EXTRACTORS` below.
"""

from . import jpmc
from .base import Holding, holdings_to_markdown, write_holdings_markdown
from .classifier import UNKNOWN, classify

_EXTRACTORS = {
    "jpmc": jpmc.extract_holdings,
    # "schwab": not yet implemented
    # "fidelity": not yet implemented
    # "1040": not yet implemented
}


def extract(pdf_path: str) -> tuple[str, list[Holding]]:
    """Classify *pdf_path* and run its matching extractor.

    Returns ``(doc_type, holdings)``. ``doc_type`` is UNKNOWN and
    ``holdings`` is empty when no extractor recognizes the document --
    callers should treat that as redaction-only, not as an error.
    """
    doc_type = classify(pdf_path)
    extractor = _EXTRACTORS.get(doc_type)
    holdings = extractor(pdf_path) if extractor else []
    return doc_type, holdings


def extract_holdings(pdf_path: str) -> list[Holding]:
    """Convenience wrapper around `extract` for callers that only need holdings."""
    _, holdings = extract(pdf_path)
    return holdings


__all__ = [
    "extract",
    "extract_holdings",
    "classify",
    "UNKNOWN",
    "Holding",
    "holdings_to_markdown",
    "write_holdings_markdown",
]
