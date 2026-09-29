#!/usr/bin/env node
import { config as loadEnv } from "dotenv";
import { Command } from "commander";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { AgentRunner } from "./agent/runner.js";
import { createProjectTools } from "./agent/tools.js";
import { loadConfig } from "./config.js";
import { AppError, publicError } from "./errors.js";
import { DeepSeekModel } from "./models/deepseek.js";
import { DemoModel } from "./models/demo.js";
import { FileStore } from "./storage/store.js";

loadEnv({ quiet: true });
const program = new Command().name("vagent").description("基于 DeepSeek + LangGraph 的视频创作 Agent 原型").version("0.1.0");

async function withAgent(session: string, demo: boolean, action: (runner: AgentRunner, signal: AbortSignal) => Promise<void>) {
  const configuration = loadConfig();
  const model = demo ? new DemoModel() : new DeepSeekModel(configuration);
  const store = await FileStore.open(configuration.home);
  const controller = new AbortController();
  const stop = () => { controller.abort(); };
  process.once("SIGINT", stop);
  try {
    console.log(`模型：${model.name}\n会话：${session}\n数据：${configuration.home}`);
    const runner = new AgentRunner({ store, model, tools: createProjectTools(), onEvent: (event) => {
      if (event.type === "model.started") console.log(`[模型步骤 ${event.step}]`);
      if (event.type === "tool.started") console.log(`[工具] ${event.name}`);
      if (event.type === "tool.completed") console.log(`[工具结果] ${event.name}: ${event.ok ? "成功" : "失败"}`);
    } });
    await action(runner, controller.signal);
  } finally { process.removeListener("SIGINT", stop); await store.close(); }
}

program.command("run").description("运行一次真实 DeepSeek 任务（会产生 API 费用）")
  .argument("<prompt>").option("-s, --session <id>", "会话 ID", "default").option("--request-id <id>", "请求去重 ID")
  .action(async (prompt: string, options: { session: string; requestId?: string }) => {
    await withAgent(options.session, false, async (runner, signal) => {
      const result = await runner.run(options.session, prompt, { requestId: options.requestId, signal });
      console.log(`\n${result.answer}\n[${result.status}] Run ${result.id}；模型 ${result.modelSteps} 步，工具 ${result.toolCalls} 次`);
      if (result.status !== "completed") process.exitCode = 1;
    });
  });

program.command("demo").description("无 Key、无网络的确定性模拟演示，保存一个测试创作产物")
  .option("-s, --session <id>", "模拟会话 ID", "demo")
  .action(async (options: { session: string }) => {
    await withAgent(options.session, true, async (runner, signal) => {
      const result = await runner.run(options.session, "演示读取项目和保存咖啡店方案。", { signal });
      console.log(`\n${result.answer}`);
      if (result.status !== "completed") process.exitCode = 1;
    });
  });

program.command("chat").description("与真实 DeepSeek 进行持续创作对话；/exit 退出")
  .option("-s, --session <id>", "会话 ID", "default")
  .action(async (options: { session: string }) => {
    if (!stdin.isTTY) throw new AppError("TTY_REQUIRED", "交互模式需要终端；自动化调用请使用 run。");
    await withAgent(options.session, false, async (runner, signal) => {
      const reader = createInterface({ input: stdin, output: stdout });
      try {
        while (!signal.aborted) {
          let prompt: string;
          try { prompt = await reader.question("\n你 > ", { signal }); }
          catch { break; }
          if (prompt.trim() === "/exit") break;
          if (!prompt.trim()) continue;
          const result = await runner.run(options.session, prompt, { signal });
          console.log(`\nAgent > ${result.answer}\n[${result.status}]`);
        }
      } finally { reader.close(); }
    });
  });

const configCommand = program.command("config").description("查看配置");
configCommand.command("show").action(() => {
  const { apiKey, ...safe } = loadConfig();
  console.log(JSON.stringify({ ...safe, apiKeyConfigured: Boolean(apiKey?.trim()), thinking: "disabled" }, null, 2));
});

program.command("inspect").description("查看本地项目、产物列表和最近 Run，不输出凭证")
  .option("-s, --session <id>", "会话 ID", "default")
  .action(async (options: { session: string }) => {
    const store = await FileStore.open(loadConfig().home);
    try {
      const snapshot = store.snapshot();
      console.log(JSON.stringify({
        project: snapshot.projects[options.session] ?? null,
        artifacts: Object.values(snapshot.artifacts).filter((a) => a.projectId === options.session),
        runs: Object.values(snapshot.runs).filter((r) => r.sessionId === options.session).map(({ id, status, modelSteps, toolCalls, errorCode }) => ({ id, status, modelSteps, toolCalls, errorCode })),
      }, null, 2));
    } finally { await store.close(); }
  });

program.parseAsync().catch((error: unknown) => {
  const safe = publicError(error);
  console.error(`[${safe.code}] ${safe.message}`);
  process.exitCode = 1;
});
