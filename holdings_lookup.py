#!/usr/bin/env python3
"""Deterministic substring/range lookup over Redactomatic ``.tables.md`` files.

Vector similarity search (as used by e.g. a RAG assistant sitting on top of
these statements) is structurally unreliable for exact-match questions:
proper nouns that don't embed distinctly ("ADP" vs "Automatic Data
Processing"), near-duplicate numeric values ($1,215.87 vs $1,215.88),
exhaustive "every row matching X" queries against large tables, and
aggregating one security across multiple account documents at once. This
script sidesteps all of that by parsing the markdown tables directly and
doing plain substring/exact/range filtering -- no embeddings, no fuzzy
matching.

``--description`` is substring-only against the Description column's
company-name text; a bare ticker like "ADP" won't substring-match
"AUTOMATIC DATA PROCESSING INC". The expectation is that Kevin expands a
ticker to its company name before querying -- but as a defense against
that not happening (model forgets, or a human runs this directly), a
zero-match description query is retried as an exact match against the
`symbol` field before giving up. `symbol` is only available for holdings
whose statement has a `<name>.holdings.json` sidecar alongside its
`.tables.md` (see document_extractors/base.py's write_holdings_json) --
without one, a bare ticker still won't resolve.

Deliberately standalone: it does not import from Redactomatic's ``app``
package (a Flask app, not a library) and instead parses whatever columns
are present in each table's own header row. That also means it isn't
tied to the exact Holding/Transaction dataclass shape -- if a column gets
added or renamed upstream, this script picks it up from the header rather
than needing a matching code change.
"""

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field


@dataclass
class Row:
    source_file: str
    kind: str  # "holdings" or "transactions"
    columns: dict = field(default_factory=dict)  # column name -> raw cell text
    symbol: str = ""  # ticker, only populated when a <name>.holdings.json sidecar exists


# Recognized table shapes, keyed by their exact header column tuple. A table
# whose header doesn't match either of these (e.g. a future vendor's layout)
# is skipped rather than guessed at.
_TABLE_KIND_BY_HEADER = {
    (
        "Account", "Page", "Description", "Quantity", "Price", "Market Value",
        "Unit Cost", "Cost Basis", "Gain/Loss",
    ): "holdings",
    (
        "Account", "Post Date", "Transaction Date", "Type", "Merchant",
        "Description", "Amount", "Category", "Reference ID",
    ): "transactions",
}

# Which column holds each row kind's principal dollar figure -- what
# --exact/--range/--min-amount/--max-amount filter against.
_MONEY_COLUMN = {"holdings": "Market Value", "transactions": "Amount"}

# Extra numeric column --sum totals alongside the money column, per kind.
_QUANTITY_COLUMN = {"holdings": "Quantity", "transactions": None}

_SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_LEADING_NUMBER_RE = re.compile(r"-?[\d,]+\.?\d*")


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def parse_amount(raw: str) -> float | None:
    """Pull the leading numeric value out of a table cell.

    Some columns carry a trailing annotation the extractor left in place
    (e.g. "816.04 ST" for a short-term gain/loss), so this matches the
    leading numeric token rather than requiring the whole cell to be a
    bare number. Returns None for blank cells (e.g. Unit Cost on a sweep
    fund row) rather than treating them as zero.
    """
    if not raw or not raw.strip():
        return None
    m = _LEADING_NUMBER_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_tables_md(path: str) -> list[Row]:
    """Parse every recognized table in *path* into a flat list of Row."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]

    rows: list[Row] = []
    filename = os.path.basename(path)
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        header = _split_row(lines[i])
        if i + 1 >= len(lines) or not _SEPARATOR_RE.match(lines[i + 1].strip()):
            i += 1
            continue
        kind = _TABLE_KIND_BY_HEADER.get(tuple(header))
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            if kind is not None:
                cells = _split_row(lines[i])
                if len(cells) == len(header):
                    rows.append(Row(source_file=filename, kind=kind, columns=dict(zip(header, cells))))
            i += 1
    return rows


def _sidecar_json_path(tables_md_path: str) -> str:
    assert tables_md_path.endswith(".tables.md")
    return tables_md_path[: -len(".tables.md")] + ".holdings.json"


def load_holdings_json(json_path: str, source_file: str) -> list[Row]:
    """Load a `<name>.holdings.json` sidecar (written by
    document_extractors/base.py's write_holdings_json) into Rows shaped
    like the markdown-table parser's, so both sources feed the same
    filtering/printing code unchanged.

    This is the only place `symbol` gets populated -- the markdown table
    never carries it (see the module docstring).
    """
    with open(json_path, encoding="utf-8") as fh:
        records = json.load(fh)
    rows: list[Row] = []
    for rec in records:
        page = rec.get("page", "")
        columns = {
            "Account": str(rec.get("account", "")),
            "Page": f"p.{page}" if page != "" else "",
            "Description": rec.get("description", ""),
            "Quantity": str(rec.get("quantity", "")),
            "Price": str(rec.get("price", "")),
            "Market Value": str(rec.get("market_value", "")),
            "Unit Cost": str(rec.get("unit_cost", "")),
            "Cost Basis": str(rec.get("cost_basis", "")),
            "Gain/Loss": str(rec.get("gain_loss", "")),
        }
        rows.append(Row(source_file=source_file, kind="holdings", columns=columns, symbol=rec.get("symbol", "")))
    return rows


def load_directory(directory: str) -> list[Row]:
    """Parse every `*.tables.md` file in *directory* into Rows.

    For a holdings file with a `<name>.holdings.json` sidecar next to it,
    the JSON is read instead of the markdown -- same row data, plus the
    `symbol` field the markdown table doesn't carry. Files without a
    sidecar (transaction tables, or holdings tables predating this sidecar)
    fall back to parsing the markdown directly.
    """
    paths = sorted(glob.glob(os.path.join(directory, "*.tables.md")))
    if not paths:
        raise SystemExit(f"No *.tables.md files found in {directory!r}")
    rows: list[Row] = []
    for path in paths:
        json_path = _sidecar_json_path(path)
        if os.path.exists(json_path):
            rows.extend(load_holdings_json(json_path, source_file=os.path.basename(path)))
        else:
            rows.extend(parse_tables_md(path))
    return rows


def parse_range(s: str) -> tuple[float, float]:
    m = re.match(r"^(-?[\d.]+)-(-?[\d.]+)$", s)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid range {s!r}, expected MIN-MAX e.g. 1215.86-1215.89")
    lo, hi = float(m.group(1)), float(m.group(2))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def row_matches(row: Row, args: argparse.Namespace) -> bool:
    if args.description is not None:
        if args.description.lower() not in row.columns.get("Description", "").lower():
            return False

    if args.account is not None:
        if row.columns.get("Account", "").strip().lower() != args.account.strip().lower():
            return False

    wants_amount_filter = any(
        v is not None for v in (args.exact, args.range, args.min_amount, args.max_amount)
    )
    if wants_amount_filter:
        money_col = _MONEY_COLUMN.get(row.kind)
        value = parse_amount(row.columns.get(money_col, "")) if money_col else None
        if value is None:
            return False
        if args.exact is not None and round(value, 2) != round(args.exact, 2):
            return False
        if args.range is not None:
            lo, hi = args.range
            if not (lo <= value <= hi):
                return False
        if args.min_amount is not None and value < args.min_amount:
            return False
        if args.max_amount is not None and value > args.max_amount:
            return False

    return True


def print_results(matches: list[Row], args: argparse.Namespace, symbol_fallback_used: bool = False) -> None:
    if symbol_fallback_used:
        print(f'No substring match on Description for "{args.description}" -- matched as a ticker symbol instead.')
        print()

    for n, row in enumerate(matches, 1):
        print(f"[{n}] source={row.source_file} kind={row.kind}")
        for col, val in row.columns.items():
            print(f"    {col}: {val}")
        if row.symbol:
            print(f"    Symbol: {row.symbol}")
        print()

    print(f"{len(matches)} match(es) across {len({r.source_file for r in matches})} file(s).")

    if args.sum and matches:
        totals: dict[str, float] = {}
        for row in matches:
            money_col = _MONEY_COLUMN.get(row.kind)
            qty_col = _QUANTITY_COLUMN.get(row.kind)
            for col in filter(None, (money_col, qty_col)):
                v = parse_amount(row.columns.get(col, ""))
                if v is not None:
                    totals[col] = totals.get(col, 0.0) + v
        if totals:
            print("Totals:")
            for col, total in totals.items():
                print(f"  {col}: {_format_total(total)}")


def _format_total(value: float) -> str:
    """Format with thousands separators, trimming trailing zeros but keeping at least 2 decimals."""
    s = f"{value:,.3f}".rstrip("0")
    if s.endswith("."):
        s += "00"
    elif len(s.split(".")[-1]) < 2:
        s += "0"
    return s


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deterministic substring/range lookup over *.tables.md files (no embeddings, no fuzzy matching).",
    )
    p.add_argument("directory", help="Directory containing one or more *.tables.md files")
    p.add_argument("--description", help="Case-insensitive substring match against the Description column")
    p.add_argument("--account", help="Exact match (case-insensitive) against the Account column")
    p.add_argument("--exact", type=float, help="Exact match (to the cent) against the row's money column")
    p.add_argument("--range", type=parse_range, help="Inclusive range MIN-MAX against the row's money column, e.g. 1215.86-1215.89")
    p.add_argument("--min-amount", type=float, help="Minimum value (inclusive) for the row's money column")
    p.add_argument("--max-amount", type=float, help="Maximum value (inclusive) for the row's money column")
    p.add_argument("--sum", action="store_true", help="Print totals (shares, market value, or amount) across all matches")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not any((args.description, args.account, args.exact is not None, args.range is not None,
                args.min_amount is not None, args.max_amount is not None)):
        print("error: at least one filter is required (--description, --account, --exact, --range, --min-amount, --max-amount)", file=sys.stderr)
        return 2

    rows = load_directory(args.directory)
    matches = [r for r in rows if row_matches(r, args)]

    # --description is substring-only against company-name text (Kevin is
    # expected to expand a ticker to its company name before querying).
    # But that's a prompting convention, not a guarantee -- if a bare
    # ticker slips through (model forgets, or a human runs the script by
    # hand) it won't substring-match a Description like "AUTOMATIC DATA
    # PROCESSING INC", even though the row is right there. So on a zero-
    # match description query, retry as an exact match against `symbol`
    # (only populated for rows loaded from a .holdings.json sidecar)
    # before giving up. Same --description flag; the caller doesn't need
    # to know which path found the row.
    symbol_fallback_used = False
    if not matches and args.description:
        target = args.description.strip().lower()
        fallback_args = argparse.Namespace(**{**vars(args), "description": None})
        matches = [
            r for r in rows
            if r.symbol.strip().lower() == target and row_matches(r, fallback_args)
        ]
        symbol_fallback_used = bool(matches)

    print_results(matches, args, symbol_fallback_used=symbol_fallback_used)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
