import type { AIMessage, BaseMessage } from "@langchain/core/messages";
import type { ToolSpec } from "../agent/tools.js";

export interface AgentModel {
  readonly name: string;
  generate(messages: BaseMessage[], tools: ToolSpec[], signal: AbortSignal): Promise<AIMessage>;
}
