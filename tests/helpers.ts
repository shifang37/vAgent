import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import { afterEach } from "vitest";
import type { AgentModel } from "../src/models/model.js";
import { FileStore } from "../src/storage/store.js";

const stores: FileStore[] = [];
const directories: string[] = [];
export async function testStore() {
  const home = await mkdtemp(join(tmpdir(), "vagent-test-"));
  directories.push(home);
  const store = await FileStore.open(home);
  stores.push(store);
  return store;
}
export class ScriptedModel implements AgentModel {
  readonly name = "test-fixture";
  calls = 0;
  constructor(private readonly step: (messages: BaseMessage[], index: number) => AIMessage | Promise<AIMessage>) {}
  async generate(messages: BaseMessage[]) { return this.step(messages, this.calls++); }
}
export function toolCall(name: string, args: Record<string, unknown>, id = "call-1") {
  return new AIMessage({ content: "", tool_calls: [{ name, args, id }] });
}
afterEach(async () => {
  for (const store of stores.splice(0)) await store.close();
  for (const directory of directories.splice(0)) {
    // Only remove the exact directories created by this test helper.
    if (!directory.startsWith(join(tmpdir(), "vagent-test-"))) throw new Error("Unexpected test directory");
    await rm(directory, { recursive: true, force: true });
  }
});
