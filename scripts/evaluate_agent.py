"""Explicit, bounded live acceptance through the same HTTP API used by the UI.

Requires a running `vagent web --mcp-local`. Never loads or prints credentials.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from vagent.quality import COUNT_METHOD, character_count, memory_conflicts

TASKS = [
    (
        "brief-and-memory",
        "这是编排验收使用的虚构项目「栖岸咖啡」。制作30秒9:16品牌短片，受众城市上班族，"
        "风格暖色自然光，约束：无旁白、保留历史版本。请先读取项目和 video-brief Skill，"
        "把目标、受众、风格及约束保存到项目记忆，保存简短执行计划，再保存一份brief。"
        "正文保持300字以内，最后报告实际保存的artifactId和版本。无需视频生成。",
        False,
    ),
    (
        "revision-skills-and-mcp",
        "继续刚才的项目，沿用持久记忆中的受众和约束，只把风格改成雨夜青蓝色。"
        "请读取当前项目、现有brief和 shot-description Skill；更新项目风格与原brief版本，"
        "不要新建另一个brief。新增一份简洁的storyboard：4个镜头分别5、7、11、7秒，"
        "调用mcp_video_shot_timing核对总时长30秒，调用mcp_video_frame_budget计算24fps、"
        "9:16画幅的帧数，把工具计算结果写入分镜。两个产物各不超过300字。最后报告ID和版本。",
        False,
    ),
    (
        "read-only-verification",
        "只读验收：读取项目记忆和现有brief的第1版及最新版，确认受众仍为城市上班族，"
        "新风格为雨夜青蓝色，30秒、9:16、无旁白约束仍保留，第1版暖色方案没有被覆盖。"
        "简短列出实际读取结果；不要修改项目、计划或产物，不需要生成新方案。",
        True,
    ),
]


def quality_findings(report):
    """Inspect saved outputs independently of the Agent's own success claims."""
    if not report.get("runs"):
        return []
    snapshot = report["runs"][-1]["snapshot"]
    findings = []
    project = snapshot["project"]
    conflicts = list(
        dict.fromkeys(
            issue
            for item in report["runs"]
            for issue in memory_conflicts(item["snapshot"]["project"], project)
        )
    )
    if conflicts:
        findings.append(
            {
                "code": "MEMORY_FIELD_CONTRADICTION",
                "field": "project",
                "detail": "；".join(conflicts),
            }
        )
    for artifact in snapshot["artifacts"]:
        for version in artifact["versions"]:
            count = character_count(version["content"])
            checked = version.get("contentCheck")
            if checked and (checked["characters"] != count or checked["method"] != COUNT_METHOD):
                findings.append(
                    {
                        "code": "CONTENT_CHECK_MISMATCH",
                        "artifactId": artifact["id"],
                        "version": version["version"],
                        "detail": "保存的统计与原文重新计数不一致。",
                    }
                )
            if count > 300:
                findings.append(
                    {
                        "code": "CONTENT_LENGTH",
                        "artifactId": artifact["id"],
                        "version": version["version"],
                        "nonWhitespaceCharacters": count,
                        "allCharacters": len(version["content"]),
                        "detail": "正文非空白字符超过本组任务的300字上限；含标点、英文、数字和 Markdown。",
                    }
                )
    return findings


async def evaluate(args):
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "transport": "real HTTP + DeepSeek + MCP stdio",
        "runs": [],
        "checks": {},
        "status": "incomplete",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def persist():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        health_response = await client.get("/api/health")
        health_response.raise_for_status()
        health = health_response.json()
        if not health.get("apiKeyConfigured") or health.get("modelKind") != "deepseek":
            raise RuntimeError("需要已配置 Key 的真实 DeepSeek Web 服务。")
        if not health.get("mcp"):
            raise RuntimeError("需要通过 --mcp-local 启动 Web。")
        report["capabilities"] = health
        client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
        created = await client.post("/api/sessions", json={})
        created.raise_for_status()
        session_id = created.json()["id"]
        report["sessionId"] = session_id
        before_readonly = None
        for name, prompt, read_only in TASKS:
            request_id = str(uuid4())
            print(
                json.dumps(
                    {"case": name, "sessionId": session_id, "requestId": request_id}, ensure_ascii=False
                ),
                flush=True,
            )
            response = await client.post(
                f"/api/sessions/{session_id}/messages",
                json={"prompt": prompt, "clientRequestId": request_id, "readOnly": read_only},
            )
            if response.status_code != 202:
                report["status"] = "request-rejected"
                report["error"] = response.json().get("error")
                persist()
                return 1
            run_id = response.json()["id"]
            started = time.monotonic()
            last_step = -1
            while True:
                response = await client.get(f"/api/runs/{run_id}")
                response.raise_for_status()
                run = response.json()
                if run["modelSteps"] != last_step:
                    print(
                        json.dumps(
                            {
                                "case": name,
                                "runId": run_id,
                                "modelSteps": run["modelSteps"],
                                "toolCalls": run["toolCalls"],
                                "status": run["status"],
                            }
                        ),
                        flush=True,
                    )
                    last_step = run["modelSteps"]
                if run["status"] != "running":
                    break
                if time.monotonic() - started > 210:
                    await client.post(f"/api/runs/{run_id}/stop", json={})
                    report["status"] = "client-timeout"
                    persist()
                    return 1
                await asyncio.sleep(0.75)
            current = (await client.get(f"/api/sessions/{session_id}")).json()
            report["runs"].append({"case": name, "run": run, "snapshot": current})
            persist()
            print(
                json.dumps(
                    {
                        "case": name,
                        "status": run["status"],
                        "errorCode": run.get("errorCode"),
                        "modelSteps": run["modelSteps"],
                        "toolCalls": run["toolCalls"],
                        "inputTokens": run["usage"]["observedInputTokens"],
                        "outputTokens": run["usage"]["observedOutputTokens"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if run["status"] != "completed":
                report["status"] = "failed"
                persist()
                return 1  # No automatic paid retries or new attempts on model failure.
            if name == "revision-skills-and-mcp":
                before_readonly = {"project": current["project"], "artifacts": current["artifacts"]}
            if read_only:
                report["checks"]["read_only_unchanged"] = before_readonly == {
                    "project": current["project"],
                    "artifacts": current["artifacts"],
                }
        project = current["project"]
        briefs = [a for a in current["artifacts"] if a["kind"] == "brief"]
        boards = [a for a in current["artifacts"] if a["kind"] == "storyboard"]
        successful_tools = {
            e["name"]
            for item in report["runs"]
            for e in item["run"]["events"]
            if e["type"] == "tool.completed" and e["ok"]
        }
        report["checks"].update(
            {
                "audience_persisted": "上班族" in project["audience"],
                "style_revised": "雨夜" in project["style"]
                and ("青蓝" in project["style"] or "蓝" in project["style"]),
                "original_brief_preserved": len(briefs) == 1 and len(briefs[0]["versions"]) >= 2,
                "storyboard_saved": bool(boards),
                "frame_calculation_used": bool(boards) and "720" in boards[0]["versions"][-1]["content"],
                "all_eight_tools_called": successful_tools
                >= {
                    "project_read",
                    "project_update",
                    "plan_update",
                    "skill_read",
                    "artifact_read",
                    "artifact_save",
                    "mcp_video_frame_budget",
                    "mcp_video_shot_timing",
                },
                "within_context_budget": all(
                    item["run"]["contextBytes"] <= health["contextBudgetBytes"] for item in report["runs"]
                ),
                "usage_recorded": all(item["run"]["usage"]["tokenUsageComplete"] for item in report["runs"]),
            }
        )
        report["status"] = "passed" if all(report["checks"].values()) else "assertion-failed"
        report["qualityFindings"] = quality_findings(report)
        if report["status"] == "passed" and report["qualityFindings"]:
            report["status"] = "quality-failed"
        report["finishedAt"] = datetime.now(UTC).isoformat()
        persist()
        print(
            json.dumps({"status": report["status"], "checks": report["checks"]}, ensure_ascii=False),
            flush=True,
        )
        return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="明确启用真实模型请求，产生 API 费用")
    parser.add_argument("--review-existing", action="store_true", help="仅检查已有输出，不调用模型")
    parser.add_argument("--url", default="http://127.0.0.1:3210")
    parser.add_argument("--output", type=Path, default=Path("output/agent-live-acceptance.json"))
    args = parser.parse_args()
    if args.review_existing:
        report = json.loads(args.output.read_text(encoding="utf-8"))
        report["qualityFindings"] = quality_findings(report)
        if (
            len(report.get("runs", [])) == len(TASKS)
            and report.get("checks")
            and all(report["checks"].values())
            and all(item["run"]["status"] == "completed" for item in report["runs"])
        ):
            report["status"] = "quality-failed" if report["qualityFindings"] else "passed"
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["qualityFindings"], ensure_ascii=False))
        raise SystemExit(0 if report.get("status") == "passed" else 1)
    if not args.live:
        parser.error("实际模型验收必须显式传入 --live。")
    raise SystemExit(asyncio.run(evaluate(args)))
