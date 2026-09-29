import { AIMessage } from "@langchain/core/messages";
import { describe, expect, it } from "vitest";
import { AgentRunner } from "../src/agent/runner.js";
import { createProjectTools } from "../src/agent/tools.js";
import { DemoModel } from "../src/models/demo.js";
import { FileStore } from "../src/storage/store.js";
import { ScriptedModel, testStore, toolCall } from "./helpers.js";

describe("LangGraph Agent loop", () => {
  it("observes real tool results before reporting a saved artifact", async () => {
    const store = await testStore();
    const runner = new AgentRunner({ store, model: new DemoModel(), tools: createProjectTools() });
    const result = await runner.run("demo", "保存方案", { requestId: "request-1" });
    expect(result.status).toBe("completed");
    expect(result.modelSteps).toBe(3);
    expect(result.toolCalls).toBe(2);
    const artifact = Object.values(store.snapshot().artifacts)[0]!;
    expect(result.answer).toContain(artifact.id);
    expect(artifact.versions[0]?.content).toContain("模拟测试产物");
    const repeated = await runner.run("demo", "保存方案", { requestId: "request-1" });
    expect(repeated.id).toBe(result.id);
    expect(Object.values(store.snapshot().artifacts)).toHaveLength(1);
    await expect(runner.run("demo", "不同需求", { requestId: "request-1" })).rejects.toMatchObject({ code: "REQUEST_CONFLICT" });
  });

  it("keeps successful artifacts when the following model request fails", async () => {
    const store = await testStore();
    const model = new ScriptedModel((_, step) => {
      if (step === 0) return toolCall("artifact_save", { kind: "brief", title: "原版", content: "暖色咖啡店" });
      throw new Error("upstream echoed secret-key-value");
    });
    const runner = new AgentRunner({ store, model, tools: createProjectTools() });
    const failed = await runner.run("default", "保存", { requestId: "retry-safe" });
    expect(failed.status).toBe("failed");
    expect(failed.answer).not.toContain("secret-key-value");
    expect(Object.values(store.snapshot().artifacts)).toHaveLength(1);
    await runner.run("default", "保存", { requestId: "retry-safe" });
    expect(model.calls).toBe(2);
  });

  it("allows ordinary replies without tools and persists complete history across restart", async () => {
    const store = await testStore();
    const runner = new AgentRunner({ store, model: new ScriptedModel(() => new AIMessage("可以先确定受众。")), tools: createProjectTools() });
    expect((await runner.run("coffee", "怎么开始？")).toolCalls).toBe(0);
    await store.close();
    const reopened = await FileStore.open(store.home);
    try {
      const model = new ScriptedModel((messages) => {
        expect(messages.some((message) => message.content === "可以先确定受众。")).toBe(true);
        return new AIMessage("继续讨论上班族受众。");
      });
      expect((await new AgentRunner({ store: reopened, model, tools: createProjectTools() }).run("coffee", "受众是上班族")).status).toBe("completed");
    } finally { await reopened.close(); }
  });

  it("returns schema errors to the model without performing the write", async () => {
    const store = await testStore();
    const model = new ScriptedModel((messages, step) => {
      if (step === 0) return toolCall("artifact_save", { kind: "brief", title: "x", content: "x", path: "C:/outside.txt" });
      expect(String(messages.at(-1)!.content)).toContain("INVALID_ARGUMENTS");
      return new AIMessage("参数不支持任意文件路径。");
    });
    const result = await new AgentRunner({ store, model, tools: createProjectTools() }).run("default", "保存到路径");
    expect(result.status).toBe("completed");
    expect(Object.values(store.snapshot().artifacts)).toHaveLength(0);
  });

  it("does not expose unregistered tools", async () => {
    const store = await testStore();
    const model = new ScriptedModel((messages, step) => {
      if (step === 0) return toolCall("shell", { command: "anything" });
      expect(String(messages.at(-1)!.content)).toContain("UNKNOWN_TOOL");
      return new AIMessage("没有 shell 工具。");
    });
    expect((await new AgentRunner({ store, model, tools: createProjectTools() }).run("default", "执行命令")).status).toBe("completed");
  });

  it("ends a tool loop at the model step budget", async () => {
    const store = await testStore();
    const model = new ScriptedModel((_, step) => toolCall("project_read", {}, `read-${step}`));
    const result = await new AgentRunner({ store, model, tools: createProjectTools(), policy: { maxSteps: 2 } }).run("default", "不停读取");
    expect(result.status).toBe("failed");
    expect(result.errorCode).toBe("STEP_LIMIT");
    expect(model.calls).toBe(2);
    expect(result.toolCalls).toBe(2);
  });

  it("rejects a whole over-budget batch and still pairs every tool message", async () => {
    const store = await testStore();
    const model = new ScriptedModel(() => new AIMessage({ content: "", tool_calls: [
      { name: "project_read", args: {}, id: "one" }, { name: "project_read", args: {}, id: "two" },
    ] }));
    const result = await new AgentRunner({ store, model, tools: createProjectTools(), policy: { maxToolCalls: 1 } }).run("default", "批量读取");
    expect(result.errorCode).toBe("TOOL_LIMIT");
    expect(result.toolCalls).toBe(0);
    expect(result.messages.filter((message) => message.type === "tool")).toHaveLength(2);
  });

  it("does not start tools after cancellation", async () => {
    const store = await testStore();
    const controller = new AbortController();
    const model = new ScriptedModel(() => {
      controller.abort();
      return toolCall("artifact_save", { kind: "brief", title: "x", content: "x" });
    });
    const result = await new AgentRunner({ store, model, tools: createProjectTools() }).run("default", "取消", { signal: controller.signal });
    expect(result.status).toBe("cancelled");
    expect(Object.values(store.snapshot().artifacts)).toHaveLength(0);
  });

  it("does not run a malformed tool call missing its ID", async () => {
    const store = await testStore();
    const model = new ScriptedModel(() => new AIMessage({ content: "", tool_calls: [{ name: "project_read", args: {} }] }));
    const result = await new AgentRunner({ store, model, tools: createProjectTools() }).run("default", "读取");
    expect(result.errorCode).toBe("INVALID_TOOL_CALL");
    expect(result.toolCalls).toBe(0);
  });
});
