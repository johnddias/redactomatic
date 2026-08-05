"""
Vendor-specific statement-table extraction, dispatched by document type.

Each brokerage/bank statement vendor lays out its tables differently
(column positions, footnote placement, transaction-row grammar), so a
single extraction routine doesn't generalize across vendors. `extract`
classifies the document from its first page and hands off to the
matching vendor module. Unrecognized documents fall back to
redaction-only: no table extraction is attempted, and an empty row
list is returned so callers can skip producing a tables sidecar.

To support a new vendor: add a marker in classifier.py, add a
`<vendor>.py` module exposing an `extract_*(pdf_path) -> list[Holding | Transaction]`
function, and register it in both `_EXTRACTORS` and `_WRITERS` below.
"""

from . import chase, jpmc
from .base import (
    Holding,
    Transaction,
    holdings_to_markdown,
    write_holdings_markdown,
    write_holdings_json,
    write_transactions_markdown,
)
from .classifier import UNKNOWN, classify

_EXTRACTORS = {
    "jpmc": jpmc.extract_holdings,
    "chase": chase.extract_transactions,
    # "schwab": not yet implemented
    # "fidelity": not yet implemented
    # "1040": not yet implemented
}

# Each vendor's rows go through a different markdown renderer (Holding vs.
# Transaction have unrelated columns), keyed the same way as _EXTRACTORS.
_WRITERS = {
    "jpmc": write_holdings_markdown,
    "chase": write_transactions_markdown,
}

# JSON sidecar writers, for doc types whose row type carries a field the
# markdown table doesn't (currently just Holding.symbol). Deliberately not
# registered for "chase": Transaction has no such gap, so there's nothing
# a JSON sidecar would add over the markdown table.
_JSON_WRITERS = {
    "jpmc": write_holdings_json,
}


def extract(pdf_path: str) -> tuple[str, list]:
    """Classify *pdf_path* and run its matching extractor.

    Returns ``(doc_type, rows)`` -- ``rows`` is a list of ``Holding`` or
    ``Transaction`` depending on ``doc_type``. ``doc_type`` is UNKNOWN and
    ``rows`` is empty when no extractor recognizes the document -- callers
    should treat that as redaction-only, not as an error.
    """
    doc_type = classify(pdf_path)
    extractor = _EXTRACTORS.get(doc_type)
    rows = extractor(pdf_path) if extractor else []
    return doc_type, rows


def write_tables_markdown(pdf_path: str, doc_type: str, rows: list) -> str | None:
    """Write *rows* to a markdown sidecar using the writer for *doc_type*.

    Returns None (and writes nothing) for an unrecognized doc_type.
    """
    writer = _WRITERS.get(doc_type)
    return writer(pdf_path, rows) if writer else None


def write_tables_json(pdf_path: str, doc_type: str, rows: list) -> str | None:
    """Write *rows* to a JSON sidecar using the writer for *doc_type*.

    Returns None (and writes nothing) for a doc_type with no JSON writer
    registered -- either unrecognized, or one whose row type has nothing
    the markdown table doesn't already carry.
    """
    writer = _JSON_WRITERS.get(doc_type)
    return writer(pdf_path, rows) if writer else None


def extract_holdings(pdf_path: str) -> list[Holding]:
    """Convenience wrapper around `extract` for callers that only need holdings."""
    _, holdings = extract(pdf_path)
    return holdings


__all__ = [
    "extract",
    "extract_holdings",
    "write_tables_markdown",
    "write_tables_json",
    "classify",
    "UNKNOWN",
    "Holding",
    "Transaction",
    "holdings_to_markdown",
    "write_holdings_markdown",
    "write_holdings_json",
    "write_transactions_markdown",
]
