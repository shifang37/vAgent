"""H6 offline comparison: what the cache split actually saves on recorded runs.

Prices already-recorded model calls twice — once with the reported cache hit/miss split,
once pricing every input token at the cache-miss rate — and prints the difference. No
network, no provider call, no writes: ``state.json`` is read through a read-only snapshot
(no migration, no crash recovery, no directory creation) and the price table is the
versioned reference table in ``vagent.pricing``. A wrong ``--home`` is an error instead
of a silently created empty store.

Usage::

    python scripts/compare_cost.py                    # whole data directory
    python scripts/compare_cost.py --project coffee --json
"""

import argparse
import json

from vagent import pricing
from vagent.errors import AppError
from vagent.storage import load_snapshot


def collect(state: dict, project: str | None) -> list[dict]:
    runs = list(state["runs"].values())
    if project in {None, "all"}:
        return runs
    return [run for run in runs if run["sessionId"] == project]


def report(runs: list[dict]) -> dict:
    aware = pricing.estimate_runs(runs)
    comparison = pricing.savings(runs)
    return {
        "pricingVersion": pricing.PRICING_VERSION,
        "priceAsOf": pricing.PRICE_AS_OF,
        "runs": len(runs),
        "pricedRuns": aware["pricedRuns"],
        "partialRuns": aware["partialRuns"],
        "unpricedRuns": aware["unpricedRuns"],
        "rows": comparison["rows"],
        "perRun": [
            {
                "id": run["id"],
                "sessionId": run["sessionId"],
                "status": run["status"],
                "cost": pricing.estimate_run(run),
            }
            for run in runs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="离线对比缓存命中的费用节省，不联网、不调用供应商")
    parser.add_argument("--home", default=".vagent", help="数据目录（默认 .vagent；必须已存在）")
    parser.add_argument("--project", nargs="?", const="all", default=None, help="只统计一个项目")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--verbose", action="store_true", help="逐 Run 列出估算")
    args = parser.parse_args()
    try:
        payload = report(collect(load_snapshot(args.home), args.project))
    except AppError as error:
        print(f"{error.code}: {error}")
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        f"pricing={payload['pricingVersion']} asOf={payload['priceAsOf']} "
        f"runs={payload['runs']} priced={payload['pricedRuns']} "
        f"partial={payload['partialRuns']} unpriced={payload['unpricedRuns']}"
    )
    if not payload["rows"]:
        print("没有可定价的 Run：模型不在参考价目表内，或没有记录到 Token 用量。")
        return 0
    print(f"{'currency':<10}{'cache-aware':>14}{'cache-blind':>14}{'saved':>12}{'saved%':>9}")
    for row in payload["rows"]:
        print(
            f"{row['currency']:<10}{row['cacheAware']:>14}{row['cacheBlind']:>14}"
            f"{row['saved']:>12}{row['savedPercent']:>9}"
        )
    if args.verbose:
        for item in payload["perRun"]:
            cost = item["cost"]
            print(f"  {item['id']} {item['sessionId']} status={cost['status']} {cost.get('amount')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
