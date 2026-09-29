import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { AgentModel } from "./model.js";

/** Deterministic integration fixture, deliberately not an imitation of an LLM. */
export class DemoModel implements AgentModel {
  readonly name = "demo (模拟模型，无 API 调用)";
  async generate(messages: BaseMessage[]): Promise<AIMessage> {
    const last = messages.at(-1)!;
    if (last.type === "human") return new AIMessage({ content: "", tool_calls: [{ id: "demo-read", name: "project_read", args: {} }] });
    if (last.type === "tool" && "name" in last && last.name === "project_read") {
      return new AIMessage({ content: "", tool_calls: [{ id: "demo-save", name: "artifact_save", args: { kind: "brief", title: "模拟演示：咖啡店雨夜", content: "【模拟测试产物】\n受众：上班族\n画面：雨夜街道，暖光咖啡店，一杯冒着热气的咖啡。\n此内容由确定性测试夹具生成，不是 DeepSeek 或视频模型生成。" } }] });
    }
    const result = JSON.parse(String(last.content)) as { ok: boolean; data?: { artifactId: string; version: number } };
    return new AIMessage(result.ok ? `【模拟演示】工具确认已保存产物 ${result.data!.artifactId}，版本 ${result.data!.version}。没有调用任何收费 API。` : "【模拟演示】保存失败，请查看工具错误。");
  }
}
