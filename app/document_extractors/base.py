"""Shared types and markdown rendering used by every vendor extractor."""

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
