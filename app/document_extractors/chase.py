"""
Chase (JPMorgan Chase Bank, N.A.) checking-statement transaction extraction
(pdfplumber).

Chase checking statements lay out transactions as one line per row across
three sections -- Deposits and Additions, ATM & Debit Card Withdrawals, and
Electronic Withdrawals -- each following "DATE DESCRIPTION AMOUNT". Unlike
the JPMS brokerage layout (jpmc.py), there's no column-position bleed to
resolve: the risk here is reading order, not column assignment. Chase's PDF
content stream does not always emit text runs in visual top-to-bottom order
(running-total lines can appear before the rows they total), so this reads
words back into visual rows by (top, x0) -- the same defense jpmc.py uses --
rather than trusting `page.extract_text()`'s string order.

The free-text DESCRIPTION column is intentionally *not* fully decomposed:
Merchant, Category and Reference ID are best-effort columns pulled out with
targeted regexes, but Description always keeps the untouched raw text so a
merchant/category miss never loses information.
"""

import re
import warnings

import pdfplumber

from .base import Transaction

# Section header text (as printed) -> (dict key, human label used for the
# Type default, and the substring that marks that section's running-total
# footer row).
_SECTIONS = {
    "DEPOSITS AND ADDITIONS": ("deposits", "Deposit", "Deposits and Additions"),
    "ATM & DEBIT CARD WITHDRAWALS": ("atm_debit", "ATM & Debit Card Withdrawal", "ATM & Debit Card Withdrawals"),
    "ELECTRONIC WITHDRAWALS": ("electronic", "Electronic Withdrawal", "Electronic Withdrawals"),
}

# One line per transaction: "MM/DD <description...> [$]NNN.NN". Non-greedy
# description group plus the end anchor is what lets this correctly split a
# description that itself contains other MM/DD-shaped tokens or dollar
# amounts (e.g. inline transaction dates, reference numbers).
_TXN_LINE_RE = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+\$?([\d,]+\.\d{2})$")

# ATM & Debit Card Withdrawals rows carry a second, inline transaction date
# after a type label ("Card Purchase", "Non-Chase ATM Withdraw", ...). Listed
# longest/most-specific first so e.g. "Card Purchase With Pin" matches before
# the plain "Card Purchase" prefix of the same string would.
_ATM_TYPE_LABELS = (
    "Card Purchase With Pin",
    "Recurring Card Purchase",
    "Non-Chase ATM Withdraw",
    "Card Purchase",
    "Payment Sent",
    "ATM Withdrawal",
    "ATM Deposit",
)
_ATM_TYPE_RE = re.compile(
    r"^(" + "|".join(re.escape(t) for t in _ATM_TYPE_LABELS) + r")\s+(\d{2}/\d{2})\s+(.*)$",
    re.IGNORECASE,
)

# Every page repeats "Account Number: <digits>" near the top of the page
# (label and value are separate text runs, printed a few px apart
# vertically and not necessarily adjacent in the PDF's word-extraction
# order -- see _detect_account). The visible digit string varies in
# length/masking across statements, so this pulls just the trailing 4
# digits as the canonical account label, matching the short form used
# elsewhere (filenames, jpmc.py's per-account tagging).
_ACCT_NUM_RE = re.compile(r"(\d{4})$")

UNKNOWN_ACCOUNT = "UNKNOWN"

_REF_LABEL_RE = re.compile(r"\b(CCD ID|PPD ID|Web ID)\s*:\s*(\S+)", re.IGNORECASE)
_CARD_REF_RE = re.compile(r"\bCard\s+(\d{3,4})\b")

_ZELLE_RE = re.compile(r"^Zelle Payment (To|From)\s+(.*)$", re.IGNORECASE)
_PAYPAL_RE = re.compile(r"^Paypal\s+(?:Purchase|Inst Xfer)\s+(.+?)(?:\s+Web ID:.*)?$", re.IGNORECASE)
_TRAILING_REFCODE_RE = re.compile(r"^[A-Za-z0-9]{6,}$")

# Some Chase statements carry invisible "*start*<section>"/"*end*<section>"
# anchor text (document-assembly bookmarks, not part of any visible column)
# at each section's boundary. Drop these before row-grouping so they can't
# get bucketed into a real transaction row and pollute its description --
# matched as a whole-word prefix so it doesn't touch legitimate merchant
# text that happens to contain a bare "*" (e.g. "Pwp Msft * E0100", "Tst*Leas").
_MARKER_WORD_RE = re.compile(r"^\*(start|end)\*")

_NOISE_PREFIXES = ("Pwp ", "Tst*", "Pp*")
# Tokens that end a merchant-name guess: transaction-type/boilerplate words
# that follow the brand name in Chase's free-text description, not part of
# the brand itself.
_MERCHANT_STOPWORDS = {
    "payments", "payment", "payroll", "purchase", "autopay", "insurance",
    "privacycom", "billpay", "collec", "ach", "inc", "llc", "bank",
    "sent", "to", "from", "pymts", "pymnt",
}
_MAX_MERCHANT_WORDS = 2

# Best-effort merchant/description keyword -> category. Deliberately small;
# anything not matched here is left blank rather than guessed.
_CATEGORY_KEYWORDS = {
    "coca cola": "Food & Drink", "olive garden": "Food & Drink", "lunchroom": "Food & Drink",
    "mexican gr": "Food & Drink", "circle k": "Gas & Fuel", "chevron": "Gas & Fuel",
    "buc-ee": "Gas & Fuel", "state farm": "Insurance", "globe life": "Insurance",
    "pac-life": "Insurance", "insurance": "Insurance", "entergy": "Utilities",
    "util pay": "Utilities", "netflix": "Subscriptions", "adobe": "Subscriptions",
    "apple.com": "Subscriptions", "nytimes": "Subscriptions", "wsj": "Subscriptions",
    "spotify": "Subscriptions", "payroll": "Income", "vmware": "Income",
    "albertsons": "Groceries", "market": "Groceries", "foods": "Groceries",
}


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


def _row_text(row):
    return " ".join(w["text"] for w in sorted(row, key=lambda w: w["x0"]))


def _reference_id(description: str) -> str:
    m = _REF_LABEL_RE.search(description)
    if m:
        return f"{m.group(1)}: {m.group(2).rstrip('.,')}"
    m = _CARD_REF_RE.search(description)
    if m:
        return f"Card {m.group(1)}"
    return ""


def _strip_trailing_refcode(text: str):
    """Drop a trailing bare reference-code token (Zelle/ACH confirmation
    number), returning (remainder, token_or_None)."""
    words = text.split()
    if words:
        last = words[-1].rstrip(",")
        if _TRAILING_REFCODE_RE.match(last) and any(c.isdigit() for c in last):
            return " ".join(words[:-1]).rstrip(",").strip(), last
    return text.rstrip(",").strip(), None


def _guess_merchant(text: str) -> str:
    """Best-effort brand/payee guess from a free-text description.

    Not a full parser -- strips a couple of known boilerplate prefixes, then
    takes the leading run of words up to the first digit-bearing token or
    known descriptor stopword (e.g. "Payments", "Inc"). Callers with a more
    specific pattern (Zelle, PayPal) should try that first.
    """
    t = text
    for pfx in _NOISE_PREFIXES:
        if t.lower().startswith(pfx.lower()):
            t = t[len(pfx):]
            break

    out = []
    for w in t.split():
        base = re.sub(r"[^A-Za-z]", "", w).lower()
        if not base or base in _MERCHANT_STOPWORDS or any(c.isdigit() for c in w):
            break
        out.append(w)
        if len(out) >= _MAX_MERCHANT_WORDS:
            break
    if out:
        return " ".join(out)
    first = t.split()
    return first[0] if first else ""


def _guess_category(merchant: str, description: str) -> str:
    haystack = f"{merchant} {description}".lower()
    for keyword, category in _CATEGORY_KEYWORDS.items():
        if keyword in haystack:
            return category
    return ""


def _merchant_and_reference(section_key: str, description: str):
    """Return (merchant, reference_id) for *description*.

    Tries the Zelle/PayPal payee patterns the spec calls out explicitly
    first (both strip a trailing confirmation code that would otherwise get
    swallowed into the merchant guess), then falls through to the generic
    heuristic plus the labelled-ID/Card-number reference lookup.
    """
    ref_id = _reference_id(description)

    m = _ZELLE_RE.match(description)
    if m:
        remainder, token = _strip_trailing_refcode(m.group(2))
        if not ref_id and token:
            ref_id = f"Zelle Ref: {token}"
        return remainder, ref_id

    m = _PAYPAL_RE.match(description)
    if m:
        return m.group(1).strip(), ref_id

    return _guess_merchant(description), ref_id


_SECTIONS_BY_KEY = {v[0]: v for v in _SECTIONS.values()}


def _infer_type(section_key: str, description: str) -> str:
    """Type default/override for the deposits and electronic sections.

    atm_debit doesn't go through here -- _parse_row derives its type
    directly from the matched _ATM_TYPE_RE label.
    """
    lower = description.lower()
    if section_key == "deposits":
        if "payroll" in lower:
            return "Payroll"
        if lower.startswith("interest payment"):
            return "Interest"
        if lower.startswith("zelle payment"):
            return "Zelle"
        return "Deposit"
    if "autopay" in lower:
        return "Autopay"
    if lower.startswith("zelle payment"):
        return "Zelle"
    if lower.startswith("paypal"):
        return "PayPal"
    if re.search(r"\bach\b", lower):
        return "ACH"
    return "Electronic Withdrawal"


def _detect_account(page) -> str:
    """Return the account number active on *page* from its "Account
    Number:" header line.

    The label ("Account", "Number:") and its digit value are separate
    text runs -- printed a few px below the label and not always adjacent
    in extract_words()'s natural order (page layout puts other header
    text between them in the underlying content stream on some pages) --
    so this locates the value by position (near, and below, the label)
    rather than by list adjacency. Returns UNKNOWN_ACCOUNT when no such
    marker is found, so a transaction is never silently mislabeled.
    """
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    label_idxs = [
        i
        for i in range(len(words) - 1)
        if words[i]["text"] == "Account" and words[i + 1]["text"] == "Number:"
    ]
    for i in label_idxs:
        label = words[i + 1]
        for w in words:
            if (
                re.fullmatch(r"\d{9,}", w["text"])
                and label["top"] <= w["top"] <= label["top"] + 20
                and w["x0"] >= label["x0"] - 20
            ):
                m = _ACCT_NUM_RE.search(w["text"])
                if m:
                    return m.group(1)
    return UNKNOWN_ACCOUNT


def _parse_row(section_key: str, account: str, post_date: str, rest: str, amount: str) -> Transaction:
    amount = amount.replace(",", "")
    transaction_date = ""
    description = rest

    if section_key == "atm_debit":
        m = _ATM_TYPE_RE.match(rest)
        if m:
            label, transaction_date, remainder = m.group(1), m.group(2), m.group(3)
            atm_type = next((t for t in _ATM_TYPE_LABELS if t.lower() == label.lower()), label)
            merchant, ref_id = _merchant_and_reference(section_key, remainder)
            return Transaction(
                account=account,
                post_date=post_date,
                transaction_date=transaction_date,
                type=atm_type,
                merchant=merchant,
                description=description,
                amount=amount,
                category=_guess_category(merchant, description),
                reference_id=ref_id,
            )
        # Unrecognized type label: fall through to the generic path below so
        # the row is still captured (raw description as fallback).
        txn_type = "ATM & Debit Card Withdrawal"
    else:
        txn_type = _infer_type(section_key, rest)

    merchant, ref_id = _merchant_and_reference(section_key, rest)
    return Transaction(
        account=account,
        post_date=post_date,
        transaction_date=transaction_date,
        type=txn_type,
        merchant=merchant,
        description=description,
        amount=amount,
        category=_guess_category(merchant, description),
        reference_id=ref_id,
    )


def _extract_page_transactions(page, account, current_section_key):
    """Parse one page's rows, returning (transactions, ending_section_key)."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    words = [w for w in words if not _MARKER_WORD_RE.match(w["text"])]
    if not words:
        return [], current_section_key

    transactions = []
    section_key = current_section_key

    for row in _group_rows(words):
        text = _row_text(row)
        stripped = text.strip()

        # Case-sensitive on purpose: section headers print in solid
        # uppercase ("DEPOSITS AND ADDITIONS"), while the CHECKING SUMMARY
        # box repeats the same words in title case ("Deposits and
        # Additions 9,359.88") ahead of the real section -- an
        # uppercased/case-insensitive compare would misfire on that line.
        header_hit = next(
            (v for header, v in _SECTIONS.items() if stripped.startswith(header)),
            None,
        )
        if header_hit:
            section_key = header_hit[0]
            continue

        if section_key is not None:
            footer_marker = _SECTIONS_BY_KEY[section_key][2]
            if stripped.lower().startswith("total") and footer_marker.lower() in stripped.lower():
                section_key = None
                continue

        if section_key is None:
            continue

        m = _TXN_LINE_RE.match(stripped)
        if not m:
            continue
        post_date, rest, amount = m.groups()
        transactions.append(_parse_row(section_key, account, post_date, rest, amount))

    return transactions, section_key


def extract_transactions(pdf_path: str) -> list[Transaction]:
    """Extract every transaction row across a Chase checking statement.

    Walks all three transaction sections (Deposits and Additions, ATM &
    Debit Card Withdrawals, Electronic Withdrawals), tracking which section
    is active across a page break -- "ELECTRONIC WITHDRAWALS (continued)"
    on a later page still matches the ELECTRONIC WITHDRAWALS header prefix,
    so no special-casing is needed for the continuation banner.

    A page that fails to parse (unexpected layout, malformed content
    stream, ...) is skipped rather than aborting the whole statement --
    one bad page shouldn't zero out every other page's transactions. The
    active section carries over unchanged past a skipped page.
    """
    transactions = []
    section_key = None
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                account = _detect_account(page)
                page_transactions, section_key = _extract_page_transactions(page, account, section_key)
            except Exception as exc:
                warnings.warn(f"{pdf_path}: skipping page {page_no}, transaction extraction failed: {exc}")
                continue
            transactions.extend(page_transactions)
    return transactions
