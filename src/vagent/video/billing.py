"""M8 E2: the hand-entered provider billing ledger (``billing-v1``).

The ledger is a **sidecar** file next to ``state.json`` (``<home>/billing.json``), not
a new Database collection: writing a bill must never force schema 10 and therefore
never invalidate the 168 frozen contract model hashes. It only records the actual
charge a human read in the provider console — nothing is scraped, no provider
``usage`` field is treated as money, and an unrecorded Job stays ``unknown`` (which
is never zero and never an error).

One record is one atomic file replacement under the Store's instance lock. There is
no cross-file transaction with ``state.json``, so the ledger never rewrites a Job:
``JobV6.cost.actual`` remains the frozen ``unknown`` record.

Currencies are never converted: CNY and USD are aggregated and shown separately, and
no cross-currency total exists (M8 D2).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from vagent.contracts import Contract, Identifier, Name, UtcTimestamp
from vagent.errors import AppError
from vagent.video.contracts import Money, parse_job

BILLING_VERSION = 1
BILLING_FILE = "billing.json"
MAX_RECORDS_PER_PROJECT = 4096
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_AMOUNT = Decimal("99999.99")
CURRENCIES = ("CNY", "USD")
CURRENCY_ORDER = ("CNY", "USD")
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "unknown", "download_failed"})
LEDGER_STATUSES = frozenset({"ok", "missing", "invalid"})
NO_FX_NOTE = "不换汇：CNY 与 USD 分别记录、分别汇总，不做汇率换算，也不生成跨币种合计。"


def _money(value) -> str:
    return format(Decimal(value), ".2f")


class BillRecord(Contract):
    """One provider charge a human verified, bound to one already-registered Job."""

    job_id: Identifier
    project_id: Identifier
    provider: Name
    currency: Literal["CNY", "USD"]
    amount: Money
    source: str = Field(min_length=1, max_length=1000)
    invoiced_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    note: str | None = Field(default=None, max_length=500)
    recorded_at: UtcTimestamp
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def traceable_charge(self):
        if Decimal(self.amount) > MAX_AMOUNT:
            raise ValueError("A recorded charge cannot exceed the ledger amount range")
        try:
            datetime.strptime(self.invoiced_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("The invoice date must be a real calendar date") from None
        if not self.source.strip():
            raise ValueError("A charge always carries a human-readable source")
        if self.note is not None and not self.note.strip():
            raise ValueError("An empty note is the same as no note")
        return self

    def business_fields(self) -> tuple:
        """Idempotency ignores ``recordedAt`` and ``revision`` (contract §4.3)."""
        return (self.currency, self.amount, self.source, self.invoiced_on, self.note)

    def as_view(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


class BillLedger(Contract):
    """``billing-v1``: one versioned map of ``jobId -> BillRecord``."""

    billing_version: int = Field(default=BILLING_VERSION, ge=1, le=1)
    records: dict[str, BillRecord] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keyed_by_job(self):
        for key, record in self.records.items():
            if key != record.job_id:
                raise ValueError("A ledger entry must be keyed by its own job ID")
        return self


def ledger_path(home) -> Path:
    return Path(home) / BILLING_FILE


def read_ledger(home) -> tuple[BillLedger | None, str]:
    """Read the sidecar ledger; a damaged file is reported, never overwritten here."""
    path = ledger_path(home)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"
    if len(data) > MAX_LEDGER_BYTES:
        return None, "invalid"
    try:
        return BillLedger.model_validate(json.loads(data)), "ok"
    except (ValueError, TypeError):
        return None, "invalid"


def ledger_bytes(ledger: BillLedger) -> bytes:
    view = ledger.model_dump(mode="json", by_alias=True)
    text = json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return (text + "\n").encode("utf-8")


def _write_ledger(home, ledger: BillLedger) -> None:
    """One record = one temporary file plus ``os.replace`` (never a partial file)."""
    path = ledger_path(home)
    try:
        validated = BillLedger.model_validate(ledger.model_dump(mode="json", by_alias=True))
        content = ledger_bytes(validated)
    except (ValueError, TypeError):
        raise AppError("BILL_RECORD_INVALID", "账单账本未写入：记录不符合 billing-v1 契约。") from None
    if len(content) > MAX_LEDGER_BYTES:
        raise AppError("BILL_RECORD_INVALID", "账单账本超过 4 MB 上限，未写入，已保留原文件。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"billing-{uuid4()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise AppError("INVALID_STORE", "账单账本未写入，请检查本地数据目录权限；原文件已保留。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _clock(clock=None) -> datetime:
    value = clock() if clock is not None else datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("The billing clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _quoted_job(state: Mapping, job_id: str) -> tuple[str, str, str, str]:
    """The Job's frozen provider/currency (never re-quoted, never converted)."""
    raw = (state.get("jobs") or {}).get(job_id)
    if raw is None:
        raise AppError("BILL_RECORD_INVALID", "这个 Job 不在当前数据目录中，账单未写入。")
    try:
        job = parse_job(raw)
    except (ValidationError, ValueError):
        raise AppError("BILL_RECORD_INVALID", "Job 记录无法读取，账单未写入。") from None
    if job.status not in TERMINAL_JOB_STATUSES:
        raise AppError("BILL_RECORD_INVALID", "只有已终态的 Job 才能录入实际扣费；在途 Job 会被继续调整。")
    cost = getattr(job, "cost", None)
    if cost is None:
        raise AppError("BILL_RECORD_INVALID", "这个 Job 没有冻结报价（模拟任务），不能录入供应商扣费。")
    return job.request.provider, cost.estimate.currency, raw["context"]["projectId"], job.status


def record_bill(
    state: Mapping,
    home,
    *,
    job_id: str,
    amount,
    currency: str,
    source: str,
    invoiced_on: str | None = None,
    note: str | None = None,
    replace: bool = False,
    expected_revision: int | None = None,
    clock=None,
    store=None,
) -> dict:
    """Record one reported actual charge, idempotently and conflict-safely.

    Returns ``{"recorded": bool, "replayed": bool, "ledgerStatus": "ok",
    "record": {...}}``. The Job and the frozen quote decide the currency; a mismatched
    currency, a missing/in-flight Job, an invalid field or a full ledger is
    ``BILL_RECORD_INVALID``; a different amount/date/source/note is ``BILL_CONFLICT``
    until the caller explicitly replaces the record with the right revision.
    """
    provider, frozen_currency, project_id, _ = _quoted_job(state, job_id)
    if currency != frozen_currency:
        raise AppError(
            "BILL_RECORD_INVALID",
            f"这个 Job 的冻结报价是 {frozen_currency}，账单币种必须与之一致（不换汇）。",
        )
    now = _clock(clock)
    note = (note or "").strip() or None
    try:
        if not isinstance(amount, str):
            amount = _money(amount)
        candidate = BillRecord(
            job_id=job_id,
            project_id=project_id,
            provider=provider,
            currency=currency,
            amount=amount,
            source=(source or "").strip(),
            invoiced_on=invoiced_on or now.date().isoformat(),
            note=note,
            recorded_at=now.isoformat(),
            revision=1,
        )
    except (ValidationError, ValueError, ArithmeticError):
        raise AppError(
            "BILL_RECORD_INVALID",
            "账单字段不符合 billing-v1 契约（金额、币种、来源、日期或备注），未写入。",
        ) from None
    if replace and expected_revision is None:
        raise AppError("BILL_RECORD_INVALID", "替换账单必须给出 --expected-revision。")
    lock = store.locked() if store is not None else nullcontext()
    with lock:
        ledger, status = read_ledger(home)
        if status == "invalid":
            raise AppError(
                "INVALID_STORE", "账单账本已损坏，未写入；请先备份或修复 billing.json，原文件已保留。"
            )
        ledger = ledger or BillLedger()
        existing = ledger.records.get(job_id)
        if existing is not None:
            if existing.business_fields() == candidate.business_fields():
                # Replay of the same bill: no write, and the timestamp never conflicts.
                return {
                    "recorded": False,
                    "replayed": True,
                    "ledgerStatus": "ok",
                    "record": existing.as_view(),
                }
            if not replace:
                raise AppError(
                    "BILL_CONFLICT",
                    "这个 Job 已有不同的已报告账单；如需更正请显式 --replace --expected-revision。",
                )
            if expected_revision != existing.revision:
                raise AppError("BILL_CONFLICT", "账单版本已变化，请重新读取后再更正。")
            revision = existing.revision + 1
        else:
            if replace:
                raise AppError("BILL_RECORD_INVALID", "没有可替换的账单记录，未写入。")
            revision = 1
        records = dict(ledger.records)
        records.pop(job_id, None)
        project_records = sum(1 for item in records.values() if item.project_id == project_id)
        if project_records >= MAX_RECORDS_PER_PROJECT:
            raise AppError(
                "BILL_RECORD_INVALID", f"每个项目最多 {MAX_RECORDS_PER_PROJECT} 条账单记录，未写入。"
            )
        record = candidate.model_copy(update={"revision": revision})
        records[job_id] = record
        _write_ledger(home, BillLedger(records=records))
    return {"recorded": True, "replayed": False, "ledgerStatus": "ok", "record": record.as_view()}


def _quote(raw: Mapping) -> tuple[str, str, str] | None:
    """``(provider, currency, amount)`` of one stored Job, or None without a quote."""
    try:
        job = parse_job(raw)
    except (ValidationError, ValueError):
        return None
    cost = getattr(job, "cost", None)
    if cost is None:
        return None
    return job.request.provider, cost.estimate.currency, cost.estimate.amount


def summarize_billing(
    state: Mapping,
    ledger: BillLedger | None = None,
    *,
    ledger_status: str = "missing",
    project_id: str | None = None,
) -> dict:
    """Per-currency estimate/reported/delta over the ledger (never one grand total)."""
    if ledger_status not in LEDGER_STATUSES:
        raise ValueError("Unknown ledger status")
    buckets: dict[str, dict] = {}

    def bucket(currency: str) -> dict:
        return buckets.setdefault(
            currency,
            {
                "currency": currency,
                "estimate": Decimal(0),
                "reported": Decimal(0),
                "reported_jobs": 0,
                "unknown_jobs": 0,
            },
        )

    recorded = ledger.records if ledger is not None else {}
    for raw in (state.get("jobs") or {}).values():
        if project_id is not None and raw["context"]["projectId"] != project_id:
            continue
        quote = _quote(raw)
        if quote is None:
            continue
        _, currency, amount = quote
        entry = bucket(currency)
        entry["estimate"] += Decimal(amount)
        if raw.get("id") in recorded:
            continue
        entry["unknown_jobs"] += 1
    for record in recorded.values():
        if project_id is not None and record.project_id != project_id:
            continue
        entry = bucket(record.currency)
        entry["reported"] += Decimal(record.amount)
        entry["reported_jobs"] += 1
    currencies = []
    for name in CURRENCY_ORDER + tuple(sorted(set(buckets) - set(CURRENCY_ORDER))):
        entry = buckets.get(name)
        if entry is None:
            continue
        reported = _money(entry["reported"]) if entry["reported_jobs"] else None
        currencies.append(
            {
                "currency": name,
                "estimate": _money(entry["estimate"]),
                "reported": reported,
                "reportedJobs": entry["reported_jobs"],
                "unknownJobs": entry["unknown_jobs"],
                "delta": _money(Decimal(reported) - entry["estimate"]) if reported is not None else None,
            }
        )
    return {
        "ledgerVersion": BILLING_VERSION,
        "ledgerStatus": ledger_status,
        "currencies": currencies,
        "reportedJobs": sum(item["reportedJobs"] for item in currencies),
        "unknownJobs": sum(item["unknownJobs"] for item in currencies),
        "notes": [NO_FX_NOTE],
    }


def billing_for(state: Mapping, home, *, project_id: str | None = None) -> dict:
    """Read the sidecar ledger of one home and summarize it; never writes anything."""
    ledger, status = read_ledger(home)
    return summarize_billing(state, ledger, ledger_status=status, project_id=project_id)


def ledger_view(
    state: Mapping,
    home,
    *,
    project_id: str | None = None,
    currency: str | None = None,
) -> dict:
    """The read-only ``show`` face: records plus the per-currency summary."""
    if currency is not None and currency not in CURRENCIES:
        raise AppError("BILL_RECORD_INVALID", "币种只能是 CNY 或 USD。")
    ledger, status = read_ledger(home)
    records = []
    if ledger is not None:
        for record in ledger.records.values():
            if project_id is not None and record.project_id != project_id:
                continue
            if currency is not None and record.currency != currency:
                continue
            records.append(record.as_view())
    records.sort(key=lambda item: (item["recordedAt"], item["jobId"]))
    return {
        "ledgerVersion": BILLING_VERSION,
        "ledgerFile": BILLING_FILE,
        "ledgerStatus": status,
        "projectId": project_id,
        "currency": currency,
        "records": records,
        "billing": summarize_billing(state, ledger, ledger_status=status, project_id=project_id),
        "notes": [NO_FX_NOTE],
    }


__all__ = [
    "BILLING_FILE",
    "BILLING_VERSION",
    "BillLedger",
    "BillRecord",
    "CURRENCIES",
    "CURRENCY_ORDER",
    "LEDGER_STATUSES",
    "MAX_AMOUNT",
    "MAX_LEDGER_BYTES",
    "MAX_RECORDS_PER_PROJECT",
    "NO_FX_NOTE",
    "TERMINAL_JOB_STATUSES",
    "billing_for",
    "ledger_bytes",
    "ledger_path",
    "ledger_view",
    "read_ledger",
    "record_bill",
    "summarize_billing",
]
