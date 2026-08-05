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
import warnings

import pdfplumber

from .base import Holding

HEADER_ANCHOR = {"Description", "Quantity"}
# "Total Account Value :" is the last real line of a page-table's holdings
# section -- immediately followed by a boilerplate legend paragraph
# ("Unless otherwise noted...", "AI Pricing Method:", ...) and then the
# "Page N of NN" footer. Bare "TOTAL " is deliberately *not* a marker here:
# asset-class subtotals ("TOTAL CASH & SWEEP FUNDS", "TOTAL EQUITY") appear
# mid-table, between one asset class's holdings and the next, so stopping
# on those would drop real holdings below them.
FOOTER_MARKERS = ("Page ", "footnotes", "Total Account Value :")

# A Quantity/Price/Market Value cell for a real holding is always a plain
# number (possibly with thousands separators and a decimal point). The
# legend paragraph printed below "Total Account Value :" is long enough
# that its words happen to land in these column x-ranges too, so without
# this check that boilerplate gets misread as a holding's figures.
_NUMERIC_CELL_RE = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?$")

# Tokens that mark the start of footnote/label content within the
# Description column -- text at or after these is never part of the
# security's name.
_FOOTNOTE_BREAK_TOKENS = {"EST", "YIELD:"}
_NOISE_TOKENS = {"I", "WILL", "SHOW"}
_PERCENT_RE = re.compile(r"^\d+(\.\d+)?%$")

# Matches "(Acct # NNNN)" style account references, three separate
# pdfplumber words: '(Acct', '#', '<number>)'. The visible number varies
# in format across documents -- a bare 4-digit reference on copies that
# already had a longer identifier redacted out, or a full dash-separated
# account number on an untouched original -- so this pulls just the
# trailing 4 digits as the canonical account label regardless of what
# precedes them, matching the short account-number form used everywhere
# else in a combined multi-account statement (filenames, statement
# sections). Mirrors redactor.py's _ACCT_NUM_RE, which locates the same
# token for row-scoped redaction of the *whole* number.
_ACCT_NUM_RE = re.compile(r"(\d{4})\)?$")

UNKNOWN_ACCOUNT = "UNKNOWN"

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
    # Description is left-aligned and deliberately given a lenient right
    # edge (see _COLUMN_MARGIN) so wrapped names aren't clipped -- x0
    # alone is the right test there. Every other column is a right-
    # aligned number/date, printed flush to its column's right edge; a
    # wide value (e.g. "1,154.29") can start far enough left that its x0
    # lands a fraction of a pixel inside the *previous* numeric column's
    # margin-widened zone, corrupting that column and silently blanking
    # its own. Bucketing those columns by center avoids that collision
    # without touching Description's overflow tolerance.
    desc = cols[0]
    if word["x0"] < desc["right"]:
        return desc["name"]
    xc = (word["x0"] + word["x1"]) / 2
    for c in cols[1:]:
        if c["left"] <= xc < c["right"]:
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
    if _PERCENT_RE.match(frag):
        return ""
    # Asset-class subtotal lines ("TOTAL CASH & SWEEP FUNDS", "TOTAL
    # EQUITY") can appear mid-table, between one asset class's holdings
    # and the next -- as continuation-only rows they'd otherwise get
    # appended onto whichever real holding preceded them.
    if frag.startswith("TOTAL "):
        return ""
    return frag


def _is_numeric_cell(text: str) -> bool:
    return bool(_NUMERIC_CELL_RE.match(text))


def _row_text(by_col, name):
    return " ".join(w["text"] for w in sorted(by_col.get(name, []), key=lambda w: w["x0"]))


def _detect_account(page) -> str:
    """Return the account number active on *page* from "(Acct # NNNN)".

    These statements repeat "JPMS LLC IRA (Acct # NNNN) ... Statement
    Period: ..." on one line of every page belonging to that account's
    section, so this is a reliable per-page marker for which of the
    (possibly several) accounts in one combined statement a given
    holdings row belongs to. Returns UNKNOWN_ACCOUNT when the page
    carries no such marker (e.g. a cover or disclosures page), so a
    holding is never silently mislabeled with the wrong account.
    """
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    for i in range(len(words) - 2):
        w1, w2, w3 = words[i], words[i + 1], words[i + 2]
        if w1["text"] == "(Acct" and w2["text"] == "#":
            m = _ACCT_NUM_RE.search(w3["text"])
            if m:
                return m.group(1)
    return UNKNOWN_ACCOUNT


def _row_bbox(row_words):
    return (
        min(w["x0"] for w in row_words),
        min(w["top"] for w in row_words),
        max(w["x1"] for w in row_words),
        max(w["bottom"] for w in row_words),
    )


def _extract_page_holdings(page, page_no, account):
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

            if frag and _is_numeric_cell(qty) and _is_numeric_cell(price) and _is_numeric_cell(mv):
                if current:
                    holdings.append(current)
                current = Holding(
                    account=account,
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

    A page that fails to parse (unexpected layout, malformed content
    stream, ...) is skipped rather than aborting the whole statement --
    one bad page shouldn't zero out every other page's holdings.
    """
    holdings = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                account = _detect_account(page)
                holdings.extend(_extract_page_holdings(page, page_no, account))
            except Exception as exc:
                warnings.warn(f"{pdf_path}: skipping page {page_no}, holdings extraction failed: {exc}")
    return holdings
