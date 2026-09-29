import { ChatDeepSeek } from "@langchain/deepseek";
import type { BaseMessage } from "@langchain/core/messages";
import { requireKey } from "../config.js";
import type { ToolSpec } from "../agent/tools.js";
import type { AgentModel } from "./model.js";

export class DeepSeekModel implements AgentModel {
  readonly name: string;
  private readonly client: ChatDeepSeek;
  constructor(options: { apiKey?: string; model?: string; fetch?: typeof fetch }) {
    this.name = options.model || "deepseek-flash";
    this.client = new ChatDeepSeek({
      apiKey: requireKey(options.apiKey), model: this.name, maxRetries: 0, timeout: 60_000, maxTokens: 4096,
      modelKwargs: { thinking: { type: "disabled" } },
      configuration: { baseURL: "https://api.deepseek.com", fetch: options.fetch },
    });
  }
  async generate(messages: BaseMessage[], tools: ToolSpec[], signal: AbortSignal) {
    return this.client.bindTools(tools).invoke(messages, { signal });
  }
}
