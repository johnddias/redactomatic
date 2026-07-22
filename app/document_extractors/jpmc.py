"""
JPMS/J.P. Morgan brokerage-statement holdings extraction (pdfplumber).

JPMS brokerage statements lay out each holdings table with a free-text
Description column that wraps onto multiple lines, interleaved with a
narrower footnote column (yield %, "Symbol: XXX") that lives in the very
same x-range as Description. A naive `extract_text()` / "nearest word"
read order jumbles these together -- e.g. a multi-lot position ends up
with its footnote text and per-lot breakdown rows spliced into the
middle of its own name, and the aggregate quantity/price/market value
picked up from the wrong line.

This module derives column boundaries per-table from the header row
actually printed on the page (so it isn't tied to one statement layout),
buckets words into rows/columns by position, and merges each holding's
wrapped description while dropping footnote-only continuation lines.
"""

import re

import pdfplumber

from .base import Holding

HEADER_ANCHOR = {"Description", "Quantity"}
FOOTER_MARKERS = ("Page ", "footnotes")

# Tokens that mark the start of footnote/label content within the
# Description column -- text at or after these is never part of the
# security's name.
_FOOTNOTE_BREAK_TOKENS = {"EST", "YIELD:"}
_NOISE_TOKENS = {"I", "WILL", "SHOW"}
_PERCENT_RE = re.compile(r"^\d+(\.\d+)?%$")

# Column gap (px) below which two adjacent header words are treated as
# one logical column (e.g. "Market" + "Value" -> "Market Value").
_COLUMN_MERGE_GAP = 8
# Margin (px) subtracted from a column's x0 when it becomes the right
# edge of the previous column, so long left-aligned text (e.g. a wrapped
# security name) isn't clipped into the next column just because it runs
# wider than the header label itself.
_COLUMN_MARGIN = 10


def _group_rows(words, y_tol=3.0):
    """Cluster words into visual rows by their vertical ('top') position."""
    rows = []
    cur = []
    cur_top = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if cur_top is None or abs(w["top"] - cur_top) <= y_tol:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur = [w]
            cur_top = w["top"]
    if cur:
        rows.append(cur)
    return rows


def _build_columns(header_row):
    """Derive named column boundaries from one header row's words."""
    header_row = sorted(header_row, key=lambda w: w["x0"])
    groups = [[header_row[0]]]
    for w in header_row[1:]:
        if w["x0"] - groups[-1][-1]["x1"] <= _COLUMN_MERGE_GAP:
            groups[-1].append(w)
        else:
            groups.append([w])

    cols = []
    for g in groups:
        cols.append(
            {
                "name": " ".join(x["text"] for x in g),
                "x0": min(x["x0"] for x in g),
                "x1": max(x["x1"] for x in g),
            }
        )

    for i, c in enumerate(cols):
        c["left"] = 0 if i == 0 else cols[i]["x0"] - _COLUMN_MARGIN
        c["right"] = 10_000 if i == len(cols) - 1 else cols[i + 1]["x0"] - _COLUMN_MARGIN
    return cols


def _bucket(word, cols):
    x0 = word["x0"]
    for c in cols:
        if c["left"] <= x0 < c["right"]:
            return c["name"]
    return cols[-1]["name"]


def _description_fragment(desc_words):
    """Text from a row's Description-column words, minus footnote content.

    Returns '' when the row carries only footnote/noise text (yield %,
    "Symbol: XXX", the "I WILL SHOW" toggle label), so it isn't appended
    to a holding's name.
    """
    tokens = []
    for w in sorted(desc_words, key=lambda w: w["x0"]):
        t = w["text"]
        if t in _NOISE_TOKENS:
            return ""
        if t in _FOOTNOTE_BREAK_TOKENS or t.startswith("Symbol:"):
            break
        tokens.append(t)
    frag = " ".join(tokens).strip()
    return "" if _PERCENT_RE.match(frag) else frag


def _row_text(by_col, name):
    return " ".join(w["text"] for w in sorted(by_col.get(name, []), key=lambda w: w["x0"]))


def _row_bbox(row_words):
    return (
        min(w["x0"] for w in row_words),
        min(w["top"] for w in row_words),
        max(w["x1"] for w in row_words),
        max(w["bottom"] for w in row_words),
    )


def _extract_page_holdings(page, page_no):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []

    rows = _group_rows(words)
    header_idxs = [i for i, r in enumerate(rows) if HEADER_ANCHOR <= {w["text"] for w in r}]

    holdings = []
    for pos, header_idx in enumerate(header_idxs):
        end_idx = header_idxs[pos + 1] if pos + 1 < len(header_idxs) else len(rows)
        cols = _build_columns(rows[header_idx])

        current = None
        for row in rows[header_idx + 1 : end_idx]:
            row_text = " ".join(w["text"] for w in row)
            if any(row_text.startswith(m) or m in row_text for m in FOOTER_MARKERS):
                break

            by_col = {}
            for w in row:
                by_col.setdefault(_bucket(w, cols), []).append(w)

            frag = _description_fragment(by_col.get("Description", []))
            qty = _row_text(by_col, "Quantity")
            price = _row_text(by_col, "Price")
            mv = _row_text(by_col, "Market Value")

            if frag and qty and price and mv:
                if current:
                    holdings.append(current)
                current = Holding(
                    page=page_no,
                    description=frag,
                    quantity=qty,
                    price=price,
                    market_value=mv,
                    unit_cost=_row_text(by_col, "Unit Cost"),
                    cost_basis=_row_text(by_col, "Cost Basis"),
                    gain_loss=_row_text(by_col, "Gain/Loss"),
                    row_bboxes=[_row_bbox(row)],
                )
            elif current is not None:
                if frag:
                    current.description += " " + frag
                current.row_bboxes.append(_row_bbox(row))

        if current:
            holdings.append(current)

    return holdings


def extract_holdings(pdf_path: str) -> list[Holding]:
    """Extract every holdings-table row across a JPMS statement.

    Handles multi-lot positions (footnote column + per-lot breakdown rows
    interleaved with the main table) by tracking, per page-table, which
    row starts a new holding (Description + Quantity + Price + Market
    Value all present) versus which rows are continuations that only
    extend the description or contribute lot-level detail already
    summarized on the holding's primary row.
    """
    holdings = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            holdings.extend(_extract_page_holdings(page, page_no))
    return holdings
