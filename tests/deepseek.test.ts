import { HumanMessage, ToolMessage } from "@langchain/core/messages";
import { expect, it } from "vitest";
import { DeepSeekModel } from "../src/models/deepseek.js";
import { createProjectTools } from "../src/agent/tools.js";

it("uses DeepSeek chat completions, disables thinking, and sends paired tool results", async () => {
  const bodies: Record<string, unknown>[] = [];
  const model = new DeepSeekModel({ apiKey: "test-placeholder", fetch: async (input, init) => {
    expect(String(input)).toBe("https://api.deepseek.com/chat/completions");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer test-placeholder");
    bodies.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({ id: "completion-test", object: "chat.completion", created: 1, model: "deepseek-flash", choices: [{ index: 0, finish_reason: bodies.length === 1 ? "tool_calls" : "stop", message: bodies.length === 1
      ? { role: "assistant", content: null, tool_calls: [{ id: "read-1", type: "function", function: { name: "project_read", arguments: "{}" } }] }
      : { role: "assistant", content: "已读取项目。" } }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }), { headers: { "content-type": "application/json" } });
  } });
  const messages = [new HumanMessage("读取项目")];
  const reply = await model.generate(messages, createProjectTools().specs(), new AbortController().signal);
  expect(reply.tool_calls?.[0]?.name).toBe("project_read");
  const final = await model.generate([...messages, reply, new ToolMessage({ content: '{"ok":true}', tool_call_id: "read-1" })], createProjectTools().specs(), new AbortController().signal);
  expect(final.content).toBe("已读取项目。");
  expect(bodies[0]?.thinking).toEqual({ type: "disabled" });
  expect((bodies[1]?.messages as { role: string; tool_call_id?: string }[]).at(-1)).toMatchObject({ role: "tool", tool_call_id: "read-1" });
});

it("fails early without a configured key", () => {
  expect(() => new DeepSeekModel({ apiKey: "" })).toThrow("DEEPSEEK_API_KEY");
});
