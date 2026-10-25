// Pure projections for the hand-entered billing ledger (billing-v1).
//
// The server is the authority: this module only turns one ledger document into rows,
// labels and a write body. It never adds two currencies together, never converts one
// into the other, and never treats a missing bill as zero. A recorded charge is the
// number a human read in the provider console; the estimate stays a separate column.

export const BILL_LEDGER_VERSION = 1;
export const BILL_FILE = "billing.json";
export const BILL_CURRENCIES = ["CNY", "USD"];

export const BILL_LEDGER_STATUS_LABELS = {
  ok: "账本可读",
  missing: "还没有账单账本",
  invalid: "账本损坏，已保留原文件",
};

export const BILL_CURRENCY_LABELS = {
  CNY: "人民币（CNY）",
  USD: "美元（USD）",
};

const BILL_ERRORS = {
  BILL_RECORD_INVALID: "账单字段或 Job 状态不符合要求，未写入账本。",
  BILL_CONFLICT: "这个 Job 已有不同的账单；更正需要显式替换并核对版本号。",
  INVALID_STORE: "账单账本无法读取或写入，已保留原文件。",
  CONFIRMATION_REQUIRED: "录入账单需要显式确认，未写入任何内容。",
  JOB_NOT_FOUND: "本地没有这个任务。",
};

export function billCurrencyLabel(currency) {
  return BILL_CURRENCY_LABELS[currency] || String(currency || "");
}

export function billLedgerStatusText(status) {
  return BILL_LEDGER_STATUS_LABELS[status] || "账单账本状态未知";
}

export function billErrorText(code) {
  return BILL_ERRORS[code] || "账单操作失败，本次没有写入任何内容。";
}

// One row per currency, in the frozen CNY → USD display order. `reported` stays
// null (not "0.00") until a human records a bill, and `delta` is null with it.
export function billingCurrencyRows(billing) {
  const currencies = Array.isArray(billing?.currencies) ? billing.currencies : [];
  return currencies.map((item) => {
    const reported = item.reported ?? null;
    const reportedJobs = Number(item.reportedJobs || 0);
    const unknownJobs = Number(item.unknownJobs || 0);
    return {
      currency: item.currency,
      label: billCurrencyLabel(item.currency),
      estimate: item.estimate ?? null,
      reported,
      delta: item.delta ?? null,
      reportedJobs,
      unknownJobs,
      state: reported == null ? "unrecorded" : deltaSign(item.delta),
      reportedText: reported == null ? "未录入（未知，不是 0）" : `${reported} ${item.currency}`,
      estimateText: item.estimate == null ? "—" : `${item.estimate} ${item.currency}`,
      deltaText:
        reported == null || item.delta == null ? "—" : `${signedAmount(item.delta)} ${item.currency}`,
      countText: `${reportedJobs} 条已录入 · ${unknownJobs} 个未录入`,
    };
  });
}

function deltaSign(delta) {
  if (delta == null) return "unrecorded";
  const value = Number(delta);
  if (Number.isNaN(value) || value === 0) return "even";
  return value > 0 ? "over" : "under";
}

function signedAmount(delta) {
  const value = Number(delta);
  if (Number.isNaN(value)) return String(delta);
  return value > 0 ? `+${delta}` : String(delta);
}

export function billingHeadline(billing) {
  if (!billing) return "还没有账单记录";
  const rows = billingCurrencyRows(billing);
  if (!rows.length) return "还没有已登记的付费任务";
  return rows
    .map((row) =>
      row.reported == null
        ? `${row.currency} 未录入账单（${row.unknownJobs} 个未知）`
        : `${row.currency} 已录入 ${row.reported}（${row.reportedJobs} 条）`,
    )
    .join(" · ");
}

export function billingNotes(billing) {
  const notes = Array.isArray(billing?.notes) ? [...billing.notes] : [];
  if (!notes.length) notes.push("不换汇：CNY 与 USD 分别记录、分别汇总，不做跨币种合计。");
  return notes;
}

export function billRecordRows(ledger) {
  const records = Array.isArray(ledger?.records) ? ledger.records : [];
  return records.map((record) => ({
    jobId: record.jobId,
    projectId: record.projectId || null,
    provider: record.provider || null,
    amount: record.amount,
    currency: record.currency,
    amountText: `${record.amount} ${record.currency}`,
    source: record.source,
    invoicedOn: record.invoicedOn,
    note: record.note || null,
    revision: Number(record.revision || 1),
    recordedAt: record.recordedAt || null,
    replaced: Number(record.revision || 1) > 1,
  }));
}

// The server rejects anything outside these bounds; the form says so first so the
// user never has to guess why nothing was written.
const AMOUNT_PATTERN = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/;
const DATE_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

export function billValidationError(draft = {}) {
  const jobId = String(draft.jobId || "").trim();
  const amount = String(draft.amount || "").trim();
  const currency = String(draft.currency || "").trim();
  const source = String(draft.source || "").trim();
  const invoicedOn = String(draft.invoicedOn || "").trim();
  const note = String(draft.note || "").trim();
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(jobId)) return "请填写本地 Job 的 ID。";
  if (!AMOUNT_PATTERN.test(amount)) return "金额必须是两位小数的十进制数字，例如 0.56。";
  if (Number(amount) > 99999.99) return "单条金额不能超过 99999.99。";
  if (!BILL_CURRENCIES.includes(currency)) return "币种只能是 CNY 或 USD。";
  if (!source) return "请填写账单来源（例如供应商控制台与日期）。";
  if (source.length > 1000) return "账单来源最多 1000 个字符。";
  if (invoicedOn && !DATE_PATTERN.test(invoicedOn)) return "账单日期必须是 YYYY-MM-DD。";
  if (invoicedOn && Number.isNaN(Date.parse(`${invoicedOn}T00:00:00Z`))) return "账单日期不是真实日期。";
  if (note.length > 500) return "备注最多 500 个字符。";
  return null;
}

// The body always carries confirm=true; nothing is written without it.
export function billRecordBody(draft = {}) {
  const body = {
    jobId: String(draft.jobId || "").trim(),
    amount: String(draft.amount || "").trim(),
    currency: String(draft.currency || "").trim(),
    source: String(draft.source || "").trim(),
    confirm: true,
  };
  const invoicedOn = String(draft.invoicedOn || "").trim();
  const note = String(draft.note || "").trim();
  if (invoicedOn) body.invoicedOn = invoicedOn;
  if (note) body.note = note;
  if (draft.replace) {
    body.replace = true;
    body.expectedRevision = Number(draft.expectedRevision);
  }
  return body;
}

export function billLedgerPath({ projectId = null } = {}) {
  const params = new URLSearchParams();
  if (projectId) params.set("projectId", projectId);
  const query = params.toString();
  return query ? `/api/usage/bill?${query}` : "/api/usage/bill";
}
