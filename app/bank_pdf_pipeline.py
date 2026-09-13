"""Read-only native-text bank statement PDF adapters.

Adapters describe only bank-specific document markers, headers, date syntax,
and amount-cell semantics.  Geometry reconstruction, normalization, stable
identity, and canonical projection remain shared.  OCR and storage mutation
are deliberately out of scope.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import unicodedata

from .transaction_plan import Transaction, resolve_transaction_identities
from .utils import canonical_hash


SOURCE = "auじぶん銀行PDF"
DEFAULT_ACCOUNT_ALIAS = "jibun-primary"
DOCOMO_SMTB_SOURCE = "ドコモSMTBネット銀行PDF"
DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS = "docomo-smtb-primary"
CHIBA_BANK_SOURCE = "千葉銀行PDF"
DEFAULT_CHIBA_BANK_ACCOUNT_ALIAS = "chiba-primary"


@dataclass(frozen=True)
class BankPdfAdapter:
    key: str
    source: str
    identity_namespace: str
    default_account_alias: str
    headers: tuple[str, str, str, str, str]
    date_pattern: re.Pattern[str]
    date_format: str
    document_markers: tuple[str, ...]
    zero_filled_opposite_amount: bool = False
    column_order: tuple[str, str, str, str, str] = (
        "date", "description", "debit", "credit", "balance",
    )
    boundary_mode: str = "vertical_lines"
    balance_order: str = "descending"


JIBUN_BANK_ADAPTER = BankPdfAdapter(
    key="au-jibun",
    source=SOURCE,
    identity_namespace="au-jibun",
    default_account_alias=DEFAULT_ACCOUNT_ALIAS,
    headers=("取引日付", "取引内容", "出金", "入金", "残高"),
    date_pattern=re.compile(r"20\d{2}/\d{2}/\d{2}"),
    date_format="%Y/%m/%d",
    document_markers=("取引日付", "取引内容"),
)

DOCOMO_SMTB_BANK_ADAPTER = BankPdfAdapter(
    key="docomo-smtb",
    source=DOCOMO_SMTB_SOURCE,
    identity_namespace="docomo-smtb",
    default_account_alias=DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS,
    headers=("日付", "内容", "出金金額", "入金金額", "残高"),
    date_pattern=re.compile(r"20\d{2}年\d{2}月\d{2}日"),
    date_format="%Y年%m月%d日",
    document_markers=("株式会社ドコモSMTBネット銀行",),
    zero_filled_opposite_amount=True,
)

CHIBA_BANK_ADAPTER = BankPdfAdapter(
    key="chiba",
    source=CHIBA_BANK_SOURCE,
    identity_namespace="chiba",
    default_account_alias=DEFAULT_CHIBA_BANK_ACCOUNT_ALIAS,
    headers=("年月日", "お支払い金額", "お預り金額", "お取引き内容", "差引残高"),
    date_pattern=re.compile(r"20\d{2}/\d{2}/\d{2}"),
    date_format="%Y/%m/%d",
    document_markers=("千葉銀行", "取引明細照会"),
    column_order=("date", "debit", "credit", "description", "balance"),
    boundary_mode="header_gaps",
    balance_order="ascending",
)


class BankPdfError(ValueError):
    pass


@dataclass(frozen=True)
class PositionedWord:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True)
class PageGeometry:
    page_number: int
    width: float
    height: float
    words: tuple[PositionedWord, ...]
    vertical_lines: tuple[float, ...]


@dataclass(frozen=True)
class BankParseIssue:
    page: int
    row: int
    reason: str


@dataclass(frozen=True)
class NormalizedBankTransaction:
    source: str
    account_alias: str
    transaction_date: str
    description: str
    signed_amount: int
    source_page: int
    source_row: int
    source_row_identity: str
    source_row_hash: str
    transaction_kind: str
    review_status: str = "accepted"

    def to_canonical(self) -> Transaction:
        business_fingerprint = canonical_hash({
            "date": self.transaction_date,
            "description": self.description,
            "signed_amount": self.signed_amount,
            "kind": self.transaction_kind,
        })[:24]
        return Transaction(
            schema_version=3,
            source=self.source,
            source_record_id=self.source_row_identity,
            transaction_date=self.transaction_date,
            merchant=self.description,
            amount_yen=self.signed_amount,
            transaction_kind=self.transaction_kind,
            payment_method="銀行口座",
            identity=self.source_row_identity,
            business_fingerprint=business_fingerprint,
            memo=f"page={self.source_page};row={self.source_row}",
            source_hash=self.source_row_hash,
        )


@dataclass(frozen=True)
class BankPdfResult:
    pages: int
    candidate_rows: int
    transactions: tuple[NormalizedBankTransaction, ...]
    issues: tuple[BankParseIssue, ...]
    balance_consistency_failures: int
    duplicate_candidates: int
    canonical_transactions: tuple[Transaction, ...]
    source: str = SOURCE
    adapter_key: str = JIBUN_BANK_ADAPTER.key

    def summary(self) -> dict[str, int | dict[str, int] | str]:
        reasons = Counter(issue.reason for issue in self.issues)
        return {
            "source": self.source,
            "adapter": self.adapter_key,
            "extraction_method": "native_pdf_words",
            "pages": self.pages,
            "candidate_rows": self.candidate_rows,
            "parsed_rows": len(self.transactions),
            "withdrawal_count": sum(
                transaction.signed_amount < 0 for transaction in self.transactions
            ),
            "deposit_count": sum(
                transaction.signed_amount > 0 for transaction in self.transactions
            ),
            "rejected_unresolved_count": len(self.issues),
            "unresolved_reasons": dict(sorted(reasons.items())),
            "duplicate_candidate_count": self.duplicate_candidates,
            "balance_consistency_failures": self.balance_consistency_failures,
            "canonical_layer_count": len(self.canonical_transactions),
        }


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _header_span(
    words: list[PositionedWord], label: str,
) -> tuple[float, float] | None:
    for start in range(len(words)):
        joined = ""
        for end in range(start, len(words)):
            joined += _text(words[end].text).replace(" ", "")
            if joined == label:
                return words[start].x0, words[end].x1
            if len(joined) >= len(label):
                break
    return None


def infer_column_boundaries(
    page: PageGeometry,
    headers: tuple[str, str, str, str, str] | None = None,
    *,
    boundary_mode: str = "vertical_lines",
) -> tuple[float, float, float, float]:
    headers = headers or JIBUN_BANK_ADAPTER.headers
    date_headers = [word for word in page.words if _text(word.text) == headers[0]]
    if not date_headers:
        raise BankPdfError("header_geometry_unresolved")
    header_top = date_headers[0].top
    band = sorted(
        (word for word in page.words if abs(word.top - header_top) <= 2.0),
        key=lambda word: word.x0,
    )
    spans = [_header_span(band, label) for label in headers]
    if any(span is None for span in spans):
        raise BankPdfError("header_geometry_unresolved")
    resolved_spans = [span for span in spans if span is not None]
    if any(
        left[1] >= right[0]
        for left, right in zip(resolved_spans, resolved_spans[1:])
    ):
        raise BankPdfError("header_geometry_unresolved")
    if boundary_mode == "header_gaps":
        return tuple(
            (left[1] + right[0]) / 2
            for left, right in zip(resolved_spans, resolved_spans[1:])
        )  # type: ignore[return-value]
    if boundary_mode != "vertical_lines":
        raise BankPdfError("column_boundary_mode_invalid")
    centers = [(span[0] + span[1]) / 2 for span in resolved_spans]
    lines = sorted(set(page.vertical_lines))
    boundaries = []
    for left, right in zip(centers, centers[1:]):
        candidates = [line for line in lines if left < line < right]
        if not candidates:
            raise BankPdfError("column_boundary_unresolved")
        midpoint = (left + right) / 2
        boundaries.append(min(candidates, key=lambda line: abs(line - midpoint)))
    return tuple(boundaries)  # type: ignore[return-value]


def _parse_money(words: list[PositionedWord], *, allow_negative: bool = False) -> int | None:
    if not words:
        return None
    value = "".join(
        _text(word.text).replace(" ", "")
        for word in sorted(words, key=lambda word: word.x0)
    ).replace("¥", "").replace("￥", "")
    pattern = r"-?[0-9][0-9,]*" if allow_negative else r"[0-9][0-9,]*"
    if not re.fullmatch(pattern, value):
        raise ValueError("amount_invalid")
    return int(value.replace(",", ""))


@dataclass(frozen=True)
class _ParsedRow:
    date: str
    description: str
    signed_amount: int
    balance: int
    page: int
    row: int


def _parse_page(
    page: PageGeometry,
    adapter: BankPdfAdapter,
) -> tuple[list[_ParsedRow], list[BankParseIssue], int]:
    try:
        boundaries = infer_column_boundaries(
            page, adapter.headers, boundary_mode=adapter.boundary_mode,
        )
    except BankPdfError as exc:
        return [], [BankParseIssue(page.page_number, 0, str(exc))], 0
    header_top = min(
        word.top for word in page.words if _text(word.text) == adapter.headers[0]
    )
    date_words = sorted(
        (
            word for word in page.words
            if word.top > header_top + 5
            and word.center_x < boundaries[0]
            and adapter.date_pattern.fullmatch(_text(word.text))
        ),
        key=lambda word: word.top,
    )
    parsed: list[_ParsedRow] = []
    issues: list[BankParseIssue] = []
    for row_number, date_word in enumerate(date_words, 1):
        row_words = [
            word for word in page.words
            if abs(word.top - date_word.top) <= 2.0
        ]
        columns = [
            [word for word in row_words if word.center_x < boundaries[0]],
            [
                word for word in row_words
                if boundaries[0] < word.center_x < boundaries[1]
            ],
            [
                word for word in row_words
                if boundaries[1] < word.center_x < boundaries[2]
            ],
            [
                word for word in row_words
                if boundaries[2] < word.center_x < boundaries[3]
            ],
            [word for word in row_words if word.center_x > boundaries[3]],
        ]
        cells = dict(zip(adapter.column_order, columns))
        description_words = cells["description"]
        debit_words = cells["debit"]
        credit_words = cells["credit"]
        balance_words = cells["balance"]
        description = _text(" ".join(
            word.text for word in sorted(description_words, key=lambda word: word.x0)
        ))
        try:
            date = datetime.strptime(
                _text(date_word.text), adapter.date_format,
            ).strftime("%Y-%m-%d")
            debit = _parse_money(debit_words)
            credit = _parse_money(credit_words)
            balance = _parse_money(balance_words, allow_negative=True)
        except ValueError as exc:
            issues.append(BankParseIssue(page.page_number, row_number, str(exc)))
            continue
        signed_amount = None
        amount_issue = None
        if adapter.zero_filled_opposite_amount:
            if debit is None or credit is None:
                amount_issue = "amount_missing"
            elif debit > 0 and credit == 0:
                signed_amount = -debit
            elif credit > 0 and debit == 0:
                signed_amount = credit
            elif debit > 0 and credit > 0:
                amount_issue = "both_debit_and_credit_positive"
            elif debit == 0 and credit == 0:
                amount_issue = "both_amounts_zero"
            else:
                amount_issue = "amount_non_positive"
        elif debit is not None and credit is not None:
            amount_issue = "both_debit_and_credit"
        elif debit is None and credit is None:
            amount_issue = "amount_missing"
        elif (debit or credit or 0) <= 0:
            amount_issue = "amount_non_positive"
        else:
            signed_amount = -debit if debit is not None else int(credit)

        if not description:
            issues.append(BankParseIssue(page.page_number, row_number, "description_missing"))
        elif amount_issue:
            issues.append(BankParseIssue(page.page_number, row_number, amount_issue))
        elif balance is None:
            issues.append(BankParseIssue(page.page_number, row_number, "balance_missing"))
        else:
            parsed.append(_ParsedRow(
                date=date,
                description=description,
                signed_amount=int(signed_amount),
                balance=balance,
                page=page.page_number,
                row=row_number,
            ))
    return parsed, issues, len(date_words)


def parse_bank_pages(
    pages: list[PageGeometry],
    *,
    adapter: BankPdfAdapter,
    account_alias: str,
    existing_identities: set[str] | None = None,
) -> BankPdfResult:
    alias = _text(account_alias)
    if not alias or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", alias):
        raise BankPdfError("account_alias_invalid")
    raw_rows: list[_ParsedRow] = []
    issues: list[BankParseIssue] = []
    candidate_rows = 0
    page_candidate_counts: dict[int, int] = {}
    for page in pages:
        rows, page_issues, page_candidates = _parse_page(page, adapter)
        raw_rows.extend(rows)
        issues.extend(page_issues)
        candidate_rows += page_candidates
        page_candidate_counts[page.page_number] = page_candidates

    materials = [
        canonical_hash({
            "source": adapter.source,
            "account_alias": alias,
            "date": row.date,
            "description": row.description,
            "signed_amount": row.signed_amount,
            "post_transaction_balance": row.balance,
        })
        for row in raw_rows
    ]
    material_counts = Counter(materials)
    occurrences: Counter[str] = Counter()
    normalized = []
    for row, material in zip(raw_rows, materials):
        identity = (
            f"bankpdf:{adapter.identity_namespace}:{alias}:{material[:24]}"
        )
        if material_counts[material] > 1:
            occurrences[material] += 1
            identity += f":{occurrences[material]:03d}"
        normalized.append(NormalizedBankTransaction(
            source=adapter.source,
            account_alias=alias,
            transaction_date=row.date,
            description=row.description,
            signed_amount=row.signed_amount,
            source_page=row.page,
            source_row=row.row,
            source_row_identity=identity,
            source_row_hash=material,
            transaction_kind="withdrawal" if row.signed_amount < 0 else "deposit",
        ))

    def adjacent(first, second) -> bool:
        return (
            first.page == second.page
            and second.row == first.row + 1
        ) or (
            second.page == first.page + 1
            and first.row == page_candidate_counts[first.page]
            and second.row == 1
        )

    if adapter.balance_order == "descending":
        balance_failures = sum(
            adjacent(newer, older)
            and newer.balance != older.balance + newer.signed_amount
            for newer, older in zip(raw_rows, raw_rows[1:])
        )
    elif adapter.balance_order == "ascending":
        balance_failures = sum(
            adjacent(older, newer)
            and newer.balance != older.balance + newer.signed_amount
            for older, newer in zip(raw_rows, raw_rows[1:])
        )
    else:
        raise BankPdfError("balance_order_invalid")
    canonical = [transaction.to_canonical() for transaction in normalized]
    resolution = resolve_transaction_identities(
        canonical,
        signature=lambda transaction: (
            transaction.source,
            transaction.source_record_id,
            transaction.transaction_date,
            transaction.merchant,
            transaction.amount_yen,
            transaction.transaction_kind,
            transaction.payment_method,
            transaction.business_fingerprint,
            transaction.source_hash,
        ),
    )
    existing = existing_identities or set()
    existing_duplicates = sum(
        transaction.identity in existing for transaction in resolution.unique
    )
    eligible = tuple(
        transaction for transaction in resolution.unique
        if transaction.identity not in existing
    )
    collision_count = sum(len(group) for group in resolution.collision_groups)
    if collision_count:
        issues.extend(
            BankParseIssue(0, 0, "identity_collision")
            for _ in range(collision_count)
        )
    return BankPdfResult(
        pages=len(pages),
        candidate_rows=candidate_rows,
        transactions=tuple(normalized),
        issues=tuple(issues),
        balance_consistency_failures=balance_failures,
        duplicate_candidates=resolution.duplicate_count + existing_duplicates,
        canonical_transactions=eligible,
        source=adapter.source,
        adapter_key=adapter.key,
    )


def parse_jibun_bank_pages(
    pages: list[PageGeometry],
    *,
    account_alias: str = DEFAULT_ACCOUNT_ALIAS,
    existing_identities: set[str] | None = None,
) -> BankPdfResult:
    return parse_bank_pages(
        pages,
        adapter=JIBUN_BANK_ADAPTER,
        account_alias=account_alias,
        existing_identities=existing_identities,
    )


def parse_docomo_smtb_bank_pages(
    pages: list[PageGeometry],
    *,
    account_alias: str = DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS,
    existing_identities: set[str] | None = None,
) -> BankPdfResult:
    return parse_bank_pages(
        pages,
        adapter=DOCOMO_SMTB_BANK_ADAPTER,
        account_alias=account_alias,
        existing_identities=existing_identities,
    )


def parse_chiba_bank_pages(
    pages: list[PageGeometry],
    *,
    account_alias: str = DEFAULT_CHIBA_BANK_ACCOUNT_ALIAS,
    existing_identities: set[str] | None = None,
) -> BankPdfResult:
    return parse_bank_pages(
        pages,
        adapter=CHIBA_BANK_ADAPTER,
        account_alias=account_alias,
        existing_identities=existing_identities,
    )


def detect_bank_pdf_adapter(pages: list[PageGeometry]) -> BankPdfAdapter:
    if not pages:
        raise BankPdfError("bank_document_empty")
    first_page_text = "".join(
        _text(word.text).replace(" ", "")
        for word in sorted(pages[0].words, key=lambda word: (word.top, word.x0))
    )
    adapters = (
        CHIBA_BANK_ADAPTER, DOCOMO_SMTB_BANK_ADAPTER, JIBUN_BANK_ADAPTER,
    )
    for adapter in adapters:
        if not all(
            marker.replace(" ", "") in first_page_text
            for marker in adapter.document_markers
        ):
            continue
        try:
            infer_column_boundaries(
                pages[0], adapter.headers,
                boundary_mode=adapter.boundary_mode,
            )
        except BankPdfError:
            continue
        return adapter
    raise BankPdfError("bank_document_unrecognized")


def materialize_native_pdf(path: str | Path) -> list[PageGeometry]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise BankPdfError("pdfplumber_required") from exc
    pages = []
    with pdfplumber.open(path) as document:
        for page_number, page in enumerate(document.pages, 1):
            if not page.chars:
                raise BankPdfError("native_text_unavailable")
            words = tuple(
                PositionedWord(
                    text=str(word["text"]),
                    x0=float(word["x0"]),
                    x1=float(word["x1"]),
                    top=float(word["top"]),
                    bottom=float(word["bottom"]),
                )
                for word in page.extract_words(x_tolerance=1, y_tolerance=2)
            )
            vertical_lines = tuple(sorted({
                round(float(line["x0"]), 3)
                for line in page.lines
                if abs(float(line["x0"]) - float(line["x1"])) < 0.5
                and abs(float(line["bottom"]) - float(line["top"])) > 10
            }))
            pages.append(PageGeometry(
                page_number=page_number,
                width=float(page.width),
                height=float(page.height),
                words=words,
                vertical_lines=vertical_lines,
            ))
    return pages


class BankPdfPipeline:
    def parse(
        self,
        path: str | Path,
        *,
        account_alias: str | None = None,
        existing_identities: set[str] | None = None,
    ) -> BankPdfResult:
        pages = materialize_native_pdf(path)
        adapter = detect_bank_pdf_adapter(pages)
        alias = (
            adapter.default_account_alias
            if account_alias is None else account_alias
        )
        return parse_bank_pages(
            pages,
            adapter=adapter,
            account_alias=alias,
            existing_identities=existing_identities,
        )

    def preview(
        self,
        path: str | Path,
        *,
        account_alias: str | None = None,
        existing_identities: set[str] | None = None,
    ) -> dict[str, int | dict[str, int] | str]:
        return self.parse(
            path,
            account_alias=account_alias,
            existing_identities=existing_identities,
        ).summary()
