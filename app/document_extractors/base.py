"""Shared types and markdown rendering used by every vendor extractor."""

import json
import pathlib
from dataclasses import dataclass, field


@dataclass
class Holding:
    account: str
    page: int
    description: str
    quantity: str
    price: str
    market_value: str
    unit_cost: str = ""
    cost_basis: str = ""
    gain_loss: str = ""
    symbol: str = ""  # ticker, captured from the "Symbol: XXX" footnote; not a markdown column, see write_holdings_json
    row_bboxes: list = field(default_factory=list)  # (x0, top, x1, bottom) per source row


def holdings_to_markdown(holdings: list[Holding]) -> str:
    lines = [
        "| Account | Page | Description | Quantity | Price | Market Value | Unit Cost | Cost Basis | Gain/Loss |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h in holdings:
        lines.append(
            f"| {h.account} | p.{h.page} | {h.description} | {h.quantity} | {h.price} | "
            f"{h.market_value} | {h.unit_cost} | {h.cost_basis} | {h.gain_loss} |"
        )
    return "\n".join(lines) + "\n"


def write_holdings_markdown(pdf_path: str, holdings: list[Holding]) -> str:
    out_path = pathlib.Path(pdf_path).with_suffix("").with_suffix(".tables.md")
    out_path.write_text(holdings_to_markdown(holdings), encoding="utf-8")
    return str(out_path)


def holdings_to_dicts(holdings: list[Holding]) -> list[dict]:
    """Serializable view of *holdings* for the JSON sidecar.

    Carries every field the markdown table does, plus `symbol` (which the
    table intentionally omits -- see write_holdings_json). Excludes
    row_bboxes: internal PDF geometry, not useful to a downstream reader.
    """
    return [
        {
            "account": h.account,
            "page": h.page,
            "description": h.description,
            "symbol": h.symbol,
            "quantity": h.quantity,
            "price": h.price,
            "market_value": h.market_value,
            "unit_cost": h.unit_cost,
            "cost_basis": h.cost_basis,
            "gain_loss": h.gain_loss,
        }
        for h in holdings
    ]


def write_holdings_json(pdf_path: str, holdings: list[Holding]) -> str:
    """Write a `<name>.holdings.json` sidecar next to the markdown table.

    Exists so a caller that needs a field the markdown table doesn't carry
    (currently just `symbol`) can read it without re-parsing the table --
    see holdings_lookup.py's ticker-symbol fallback.
    """
    out_path = pathlib.Path(pdf_path).with_suffix("").with_suffix(".holdings.json")
    out_path.write_text(json.dumps(holdings_to_dicts(holdings), indent=2), encoding="utf-8")
    return str(out_path)


@dataclass
class Transaction:
    account: str
    post_date: str
    transaction_date: str
    type: str
    merchant: str
    description: str
    amount: str
    category: str = ""
    reference_id: str = ""


def transactions_to_markdown(transactions: list[Transaction]) -> str:
    lines = [
        "| Account | Post Date | Transaction Date | Type | Merchant | Description | Amount | Category | Reference ID |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in transactions:
        lines.append(
            f"| {t.account} | {t.post_date} | {t.transaction_date} | {t.type} | {t.merchant} | "
            f"{t.description} | {t.amount} | {t.category} | {t.reference_id} |"
        )
    return "\n".join(lines) + "\n"


def write_transactions_markdown(pdf_path: str, transactions: list[Transaction]) -> str:
    out_path = pathlib.Path(pdf_path).with_suffix("").with_suffix(".tables.md")
    out_path.write_text(transactions_to_markdown(transactions), encoding="utf-8")
    return str(out_path)
