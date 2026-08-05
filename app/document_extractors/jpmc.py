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


def _extract_symbol(desc_words):
    """Return the ticker symbol from a "Symbol: XXX" footnote, or ''.

    The footnote prints as two separate pdfplumber words -- "Symbol:" then
    the ticker -- sharing the Description column's x-range on their own
    row, sandwiched between a holding's primary row and the next one's
    (confirmed against real statement word positions: "Symbol:" and the
    ticker land on the same `top`, distinct from the holding-name row's
    `top`). Also handles a concatenated "Symbol:ADP" token, in case some
    statement layout doesn't split it the same way.
    """
    tokens = sorted(desc_words, key=lambda w: w["x0"])
    for i, w in enumerate(tokens):
        t = w["text"]
        if t == "Symbol:":
            return tokens[i + 1]["text"].strip() if i + 1 < len(tokens) else ""
        if t.startswith("Symbol:"):
            return t[len("Symbol:"):].strip()
    return ""


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


# A holdings table's own column set, distinct from HEADER_ANCHOR: a page
# from an unrelated table (e.g. an "Activity"/cost-basis-detail section)
# can also print a header row containing "Description" and "Quantity" --
# HEADER_ANCHOR alone doesn't rule that out. Without a Price and Market
# Value column, though, the primary-row test in _process_rows can never
# fire (both cells read as blank, so _is_numeric_cell always fails),
# which means a holding carried into that section's first row would never
# get closed out -- every row would silently read as a continuation and
# keep extending it for the entire section. Requiring both columns before
# a carried holding is ever seeded into a new header's section rules that
# out structurally, not just by hoping the real content never triggers it.
_HOLDINGS_TABLE_COLUMNS = {"Price", "Market Value"}


def _is_recognized_continuation(frag, symbol, current):
    """Is this row an unambiguous continuation of *current*'s holding?

    Only three shapes qualify: a Symbol: footnote, a lot-detail row that
    carries no Description text at all (its date/quantity/etc. live in
    other columns), or an exact reprint of the holding's own name. Used
    only for the first row(s) of a holding carried across a page break --
    same-page continuation rows are already known, by construction, to
    belong to the one open table section between two headers, so they
    don't need this narrower check.
    """
    if symbol:
        return True
    if not frag:
        return True
    # The reprint marker only repeats the security's first name line, not
    # the full accumulated description -- a holding whose name wrapped
    # onto a second line before the page break (e.g. "BAE SYSTEMS PLC" /
    # "SPONSORED ADR") only gets "BAE SYSTEMS PLC" reprinted on the next
    # page, not the whole wrapped string. A prefix match, not just an
    # exact one, is what the real reprint convention needs.
    normalized_current = current.description.strip().lower()
    normalized_frag = frag.strip().lower()
    return normalized_frag == normalized_current or normalized_current.startswith(normalized_frag)


def _process_rows(rows, cols, account, page_no, current, carried=False):
    """Run *rows* through the primary-row/continuation-row test against *cols*.

    *current* is the possibly-None holding already open when this call
    starts. *carried* marks that *current* (if any) came from the previous
    page's page-break rather than from earlier in this same section: its
    first continuation-only row must additionally pass
    _is_recognized_continuation before being merged in, since nothing yet
    confirms this section's *content* -- as opposed to its column shape,
    already checked by the caller -- actually continues that holding
    rather than being an unrelated row that merely isn't shaped like a
    fresh primary row either.

    Returns (finalized, current): `finalized` is every holding that was
    closed out because a later row's own Description + Quantity + Price +
    Market Value started a new one; the trailing `current` is left open for
    the caller to either continue extending, finalize, or carry forward.
    """
    finalized = []
    for row in rows:
        row_text = " ".join(w["text"] for w in row)
        if any(row_text.startswith(m) or m in row_text for m in FOOTER_MARKERS):
            break

        by_col = {}
        for w in row:
            by_col.setdefault(_bucket(w, cols), []).append(w)

        frag = _description_fragment(by_col.get("Description", []))
        symbol = _extract_symbol(by_col.get("Description", []))
        qty = _row_text(by_col, "Quantity")
        price = _row_text(by_col, "Price")
        mv = _row_text(by_col, "Market Value")

        if frag and _is_numeric_cell(qty) and _is_numeric_cell(price) and _is_numeric_cell(mv):
            if current:
                finalized.append(current)
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
                symbol=symbol,
                row_bboxes=[_row_bbox(row)],
            )
            carried = False
        elif current is not None:
            if carried and not _is_recognized_continuation(frag, symbol, current):
                # This row isn't recognizable as the carried holding's
                # continuation -- close the carry out as-is and keep
                # processing the rest of the section normally (a `break`
                # here would silently discard every remaining row/holding
                # in this table, not just decline this one row).
                finalized.append(current)
                current = None
                carried = False
                continue
            carried = False
            # A continuation-only row's Description text is normally a
            # wrapped fragment to append -- except when it's a repeat of
            # what's accumulated so far (the whole thing, or just its
            # first line -- see _is_recognized_continuation), which
            # happens when a page-break continuation reprints the
            # security's name as a "still talking about this one" marker
            # (the same convention as a mid-page "EQUITY (continued)"
            # section header) rather than contributing new text.
            if frag:
                normalized_current = current.description.strip().lower()
                normalized_frag = frag.strip().lower()
                if normalized_frag != normalized_current and not normalized_current.startswith(normalized_frag):
                    current.description += " " + frag
            if symbol and not current.symbol:
                current.symbol = symbol
            current.row_bboxes.append(_row_bbox(row))

    return finalized, current


def _extract_page_holdings(page, page_no, account, carry):
    """Extract holdings from one page, folding in a holding carried from
    the previous page's page-break.

    *carry* is a caller-owned {"holding": Holding | None, "account": str |
    None} dict, mutated in place. JPMorgan reprints a split holding's
    security name at the top of the continuation page -- Symbol: footnote
    and/or per-lot breakdown rows follow, all under a freshly reprinted
    column header (same convention as a mid-page "EQUITY (continued)"
    section header) rather than before it. So the carried holding is only
    ever seeded into the *first* header section found on this page, and
    only when that header's own columns still look like a holdings table
    (see _HOLDINGS_TABLE_COLUMNS) and the account still matches; whatever
    that section leaves `current` as -- extended, or closed out by a
    genuine new primary row -- is finalized before returning, rather than
    carried past a second page boundary. Anything that disqualifies the
    carry (no header at all on this page, a mismatched account, or a
    first header whose shape doesn't match a holdings table) closes it out
    immediately instead, so it can never drift into an unrelated section.
    """
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []

    rows = _group_rows(words)
    header_idxs = [i for i, r in enumerate(rows) if HEADER_ANCHOR <= {w["text"] for w in r}]

    holdings = []
    seed = None
    if carry["holding"] is not None:
        if header_idxs and carry["account"] == account:
            seed = carry["holding"]
        else:
            holdings.append(carry["holding"])
        carry["holding"] = None
        carry["account"] = None

    for pos, header_idx in enumerate(header_idxs):
        end_idx = header_idxs[pos + 1] if pos + 1 < len(header_idxs) else len(rows)
        cols = _build_columns(rows[header_idx])
        col_names = {c["name"] for c in cols}

        current = None
        carried = False
        if pos == 0 and seed is not None:
            if _HOLDINGS_TABLE_COLUMNS <= col_names:
                current = seed
                carried = True
            else:
                holdings.append(seed)
            seed = None

        finalized, current = _process_rows(
            rows[header_idx + 1 : end_idx], cols, account, page_no, current, carried=carried
        )
        holdings.extend(finalized)

        is_last_section = pos == len(header_idxs) - 1
        if is_last_section:
            # May still gain continuation rows from the top of the next
            # page, so don't finalize it yet -- extract_holdings carries
            # it forward instead of appending it here.
            carry["holding"] = current
            carry["account"] = account if current is not None else None
        elif current is not None:
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

    Also carries a still-open holding across a page break (see
    _extract_page_holdings), since JPMS statements sometimes split one
    holding's aggregate row and its Symbol:/per-lot continuation rows
    across two pages rather than keeping them on one.

    A page that fails to parse (unexpected layout, malformed content
    stream, ...) is skipped rather than aborting the whole statement --
    one bad page shouldn't zero out every other page's holdings.
    """
    holdings = []
    carry = {"holding": None, "account": None}
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                account = _detect_account(page)
                holdings.extend(_extract_page_holdings(page, page_no, account, carry))
            except Exception as exc:
                warnings.warn(f"{pdf_path}: skipping page {page_no}, holdings extraction failed: {exc}")
    if carry["holding"] is not None:
        holdings.append(carry["holding"])
    return holdings
