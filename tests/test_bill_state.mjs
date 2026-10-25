import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BILL_CURRENCIES,
  BILL_LEDGER_VERSION,
  billCurrencyLabel,
  billErrorText,
  billLedgerPath,
  billLedgerStatusText,
  billRecordBody,
  billRecordRows,
  billValidationError,
  billingCurrencyRows,
  billingHeadline,
  billingNotes,
} from "../web/bill-state.js";

const BILLING = {
  ledgerVersion: BILL_LEDGER_VERSION,
  ledgerStatus: "ok",
  currencies: [
    {
      currency: "CNY",
      estimate: "3.00",
      reported: null,
      reportedJobs: 0,
      unknownJobs: 1,
      delta: null,
    },
    {
      currency: "USD",
      estimate: "0.56",
      reported: "0.56",
      reportedJobs: 1,
      unknownJobs: 0,
      delta: "0.00",
    },
  ],
  reportedJobs: 1,
  unknownJobs: 1,
  notes: ["不换汇：CNY 与 USD 分别记录、分别汇总，不做汇率换算，也不生成跨币种合计。"],
};

const LEDGER = {
  ledgerVersion: BILL_LEDGER_VERSION,
  ledgerFile: "billing.json",
  ledgerStatus: "ok",
  projectId: null,
  currency: null,
  records: [
    {
      jobId: "job-1",
      projectId: "coffee",
      provider: "kling",
      currency: "USD",
      amount: "0.56",
      source: "Kling 控制台 2026-10-25 账单",
      invoicedOn: "2026-10-25",
      note: null,
      recordedAt: "2026-10-25T09:00:00+00:00",
      revision: 1,
    },
    {
      jobId: "job-2",
      projectId: "coffee",
      provider: "wan",
      currency: "CNY",
      amount: "3.00",
      source: "阿里云账单 2026-10-25",
      invoicedOn: "2026-10-25",
      note: "含税",
      recordedAt: "2026-10-25T10:00:00+00:00",
      revision: 2,
    },
  ],
  billing: BILLING,
  notes: BILLING.notes,
};

test("currency rows keep the server order and never merge currencies", () => {
  const rows = billingCurrencyRows(BILLING);
  assert.deepEqual(
    rows.map((row) => row.currency),
    ["CNY", "USD"],
  );
  assert.equal(rows[0].reported, null);
  assert.equal(rows[0].reportedText, "未录入（未知，不是 0）");
  assert.equal(rows[0].deltaText, "—");
  assert.equal(rows[0].state, "unrecorded");
  assert.equal(rows[1].reportedText, "0.56 USD");
  assert.equal(rows[1].estimateText, "0.56 USD");
  assert.equal(rows[1].deltaText, "0.00 USD");
  assert.equal(rows[1].state, "even");
  assert.equal(rows[1].countText, "1 条已录入 · 0 个未录入");
  assert.equal(
    rows.some((row) => "total" in row),
    false,
  );
  assert.deepEqual(billingCurrencyRows({ currencies: [] }), []);
  assert.deepEqual(billingCurrencyRows(null), []);
});

test("a delta is signed and never presented as a currency conversion", () => {
  const rows = billingCurrencyRows({
    currencies: [
      { currency: "USD", estimate: "0.56", reported: "0.70", reportedJobs: 1, unknownJobs: 0, delta: "0.14" },
      { currency: "USD", estimate: "0.56", reported: "0.40", reportedJobs: 1, unknownJobs: 0, delta: "-0.16" },
    ],
  });
  assert.equal(rows[0].deltaText, "+0.14 USD");
  assert.equal(rows[0].state, "over");
  assert.equal(rows[1].deltaText, "-0.16 USD");
  assert.equal(rows[1].state, "under");
});

test("the headline and notes describe the ledger without inventing totals", () => {
  assert.equal(billingHeadline(BILLING), "CNY 未录入账单（1 个未知） · USD 已录入 0.56（1 条）");
  assert.equal(billingHeadline({ currencies: [] }), "还没有已登记的付费任务");
  assert.equal(billingHeadline(null), "还没有账单记录");
  assert.deepEqual(billingNotes(BILLING), BILLING.notes);
  const notes = billingNotes({});
  assert.equal(notes.length, 1);
  assert.match(notes[0], /不换汇/);
  assert.equal(billLedgerStatusText("missing"), "还没有账单账本");
  assert.equal(billLedgerStatusText("invalid"), "账本损坏，已保留原文件");
  assert.equal(billLedgerStatusText("weird"), "账单账本状态未知");
});

test("record rows expose version and source for a traceable ledger", () => {
  const rows = billRecordRows(LEDGER);
  assert.deepEqual(
    rows.map((row) => row.jobId),
    ["job-1", "job-2"],
  );
  assert.deepEqual(
    rows.map((row) => row.amountText),
    ["0.56 USD", "3.00 CNY"],
  );
  assert.deepEqual(
    rows.map((row) => row.replaced),
    [false, true],
  );
  assert.equal(rows[1].note, "含税");
  assert.equal(rows[0].source, "Kling 控制台 2026-10-25 账单");
  assert.deepEqual(billRecordRows({ records: [] }), []);
  assert.deepEqual(billRecordRows(null), []);
});

test("the write body always confirms and never invents fields", () => {
  assert.deepEqual(
    billRecordBody({
      jobId: " job-1 ",
      amount: " 0.56 ",
      currency: "USD",
      source: " Kling 控制台 2026-10-25 账单 ",
      invoicedOn: "",
      note: "",
    }),
    {
      jobId: "job-1",
      amount: "0.56",
      currency: "USD",
      source: "Kling 控制台 2026-10-25 账单",
      confirm: true,
    },
  );
  assert.deepEqual(
    billRecordBody({
      jobId: "job-1",
      amount: "0.60",
      currency: "USD",
      source: "Kling",
      invoicedOn: "2026-10-25",
      note: "含税",
      replace: true,
      expectedRevision: "2",
    }),
    {
      jobId: "job-1",
      amount: "0.60",
      currency: "USD",
      source: "Kling",
      invoicedOn: "2026-10-25",
      note: "含税",
      confirm: true,
      replace: true,
      expectedRevision: 2,
    },
  );
});

test("validation catches what the server would reject", () => {
  const valid = {
    jobId: "job-1",
    amount: "0.56",
    currency: "USD",
    source: "Kling 控制台 2026-10-25 账单",
    invoicedOn: "2026-10-25",
    note: "",
  };
  assert.equal(billValidationError(valid), null);
  assert.match(billValidationError({ ...valid, jobId: "" }), /Job/);
  assert.match(billValidationError({ ...valid, jobId: "../etc" }), /Job/);
  assert.match(billValidationError({ ...valid, amount: "1.234" }), /金额/);
  assert.match(billValidationError({ ...valid, amount: "-1" }), /金额/);
  assert.match(billValidationError({ ...valid, amount: "100000.00" }), /99999.99/);
  assert.match(billValidationError({ ...valid, currency: "EUR" }), /币种/);
  assert.match(billValidationError({ ...valid, source: "   " }), /来源/);
  assert.match(billValidationError({ ...valid, invoicedOn: "2026-13-01" }), /日期/);
  assert.match(billValidationError({ ...valid, note: "x".repeat(501) }), /备注/);
  assert.match(billValidationError({}), /Job/);
});

test("ledger paths and labels stay local and explicit", () => {
  assert.equal(billLedgerPath(), "/api/usage/bill");
  assert.equal(billLedgerPath({ projectId: "coffee" }), "/api/usage/bill?projectId=coffee");
  assert.deepEqual(BILL_CURRENCIES, ["CNY", "USD"]);
  assert.equal(billCurrencyLabel("USD"), "美元（USD）");
  assert.equal(billCurrencyLabel("EUR"), "EUR");
  assert.equal(billErrorText("BILL_CONFLICT"), "这个 Job 已有不同的账单；更正需要显式替换并核对版本号。");
  assert.equal(billErrorText("SOMETHING_NEW"), "账单操作失败，本次没有写入任何内容。");
});
