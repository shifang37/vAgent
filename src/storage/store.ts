import { randomUUID, createHash } from "node:crypto";
import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { join } from "node:path";
import type { StoredMessage } from "@langchain/core/messages";
import { z } from "zod";
import { AppError } from "../errors.js";
import { assertId } from "../config.js";

export type ToolResult = { ok: true; data: unknown } | { ok: false; error: { code: string; message: string } };
export type RunStatus = "running" | "completed" | "failed" | "cancelled" | "interrupted";
export interface Project {
  id: string;
  revision: number;
  goal: string;
  audience: string;
  style: string;
  constraints: string[];
  plan: { text: string; status: "pending" | "in_progress" | "completed" }[];
}
export interface ArtifactVersion { version: number; title: string; content: string; createdAt: string }
export interface Artifact { id: string; projectId: string; kind: "brief" | "script" | "storyboard"; versions: ArtifactVersion[] }
export interface Session { id: string; messages: StoredMessage[] }
export interface RunRecord {
  id: string;
  sessionId: string;
  requestId: string;
  prompt: string;
  model: string;
  status: RunStatus;
  messages: StoredMessage[];
  modelSteps: number;
  toolCalls: number;
  inputTokens: number;
  outputTokens: number;
  answer: string;
  errorCode?: string;
  createdAt: string;
  updatedAt: string;
}
export interface Database {
  schemaVersion: 1;
  projects: Record<string, Project>;
  sessions: Record<string, Session>;
  artifacts: Record<string, Artifact>;
  runs: Record<string, RunRecord>;
  operations: Record<string, { fingerprint: string; result: ToolResult }>;
}

const storedMessage = z.object({ type: z.string(), data: z.looseObject({
  content: z.string(), role: z.string().optional(), name: z.string().optional(), tool_call_id: z.string().optional(),
}) }).transform((message): StoredMessage => ({ ...message, data: {
  ...message.data, role: message.data.role, name: message.data.name, tool_call_id: message.data.tool_call_id,
} }));
const databaseSchema = z.object({
  schemaVersion: z.literal(1),
  projects: z.record(z.string(), z.object({
    id: z.string(), revision: z.number().int().nonnegative(), goal: z.string(), audience: z.string(), style: z.string(),
    constraints: z.array(z.string()), plan: z.array(z.object({ text: z.string(), status: z.enum(["pending", "in_progress", "completed"]) })),
  })),
  sessions: z.record(z.string(), z.object({ id: z.string(), messages: z.array(storedMessage) })),
  artifacts: z.record(z.string(), z.object({
    id: z.string(), projectId: z.string(), kind: z.enum(["brief", "script", "storyboard"]),
    versions: z.array(z.object({ version: z.number().int().positive(), title: z.string(), content: z.string(), createdAt: z.string() })).min(1),
  })),
  runs: z.record(z.string(), z.object({
    id: z.string(), sessionId: z.string(), requestId: z.string(), prompt: z.string(), model: z.string(),
    status: z.enum(["running", "completed", "failed", "cancelled", "interrupted"]), messages: z.array(storedMessage),
    modelSteps: z.number(), toolCalls: z.number(), inputTokens: z.number(), outputTokens: z.number(), answer: z.string(),
    errorCode: z.string().optional(), createdAt: z.string(), updatedAt: z.string(),
  })),
  operations: z.record(z.string(), z.object({ fingerprint: z.string(), result: z.discriminatedUnion("ok", [
    z.object({ ok: z.literal(true), data: z.unknown() }),
    z.object({ ok: z.literal(false), error: z.object({ code: z.string(), message: z.string() }) }),
  ]) })),
});

export class FileStore {
  private tail: Promise<unknown> = Promise.resolve();
  private closed = false;
  private constructor(public readonly home: string, private state: Database) {}

  static async open(home: string): Promise<FileStore> {
    await mkdir(home, { recursive: true, mode: 0o700 });
    const lock = join(home, "instance.lock");
    try {
      const file = await open(lock, "wx", 0o600);
      try { await file.writeFile(JSON.stringify({ pid: process.pid, createdAt: new Date().toISOString() })); }
      finally { await file.close(); }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new AppError("STORE_LOCKED", "数据目录已被锁定。确认没有 vagent 进程后，再手动移除该目录的 instance.lock。");
      }
      throw error;
    }
    try {
      let state: Database;
      try {
        const raw = await readFile(join(home, "state.json"), "utf8");
        state = databaseSchema.parse(JSON.parse(raw)) as Database;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw new AppError("INVALID_STORE", "本地状态文件损坏或版本不支持，已保留原文件，未重置数据。");
        state = { schemaVersion: 1, projects: {}, sessions: {}, artifacts: {}, runs: {}, operations: {} };
      }
      const store = new FileStore(home, state);
      await store.transaction((draft) => {
        for (const run of Object.values(draft.runs)) if (run.status === "running") run.status = "interrupted";
      });
      return store;
    } catch (error) { await unlink(lock); throw error; }
  }

  snapshot(): Database { return structuredClone(this.state); }

  async transaction<T>(mutate: (draft: Database) => T): Promise<T> {
    if (this.closed) throw new AppError("STORE_CLOSED", "状态存储已关闭。");
    const result = this.tail.then(async () => {
      const draft = structuredClone(this.state);
      const value = mutate(draft);
      const temporary = join(this.home, `state-${randomUUID()}.tmp`);
      const file = await open(temporary, "wx", 0o600);
      try { await file.writeFile(JSON.stringify(draft, null, 2)); await file.sync(); }
      finally { await file.close(); }
      try { await rename(temporary, join(this.home, "state.json")); }
      catch (error) { await unlink(temporary).catch(() => undefined); throw error; }
      this.state = draft;
      return structuredClone(value);
    });
    this.tail = result.catch(() => undefined);
    return result;
  }

  async ensureSession(id: string): Promise<void> {
    assertId(id);
    await this.transaction((draft) => {
      draft.sessions[id] ??= { id, messages: [] };
      draft.projects[id] ??= { id, revision: 0, goal: "", audience: "", style: "", constraints: [], plan: [] };
    });
  }

  async operation(key: string, name: string, args: unknown, mutate: (draft: Database) => unknown): Promise<ToolResult> {
    const fingerprint = createHash("sha256").update(JSON.stringify({ name, args })).digest("hex");
    return this.transaction((draft): ToolResult => {
      const existing = draft.operations[key];
      if (existing) {
        if (existing.fingerprint !== fingerprint) return { ok: false, error: { code: "OPERATION_CONFLICT", message: "同一调用 ID 不能用于不同操作。" } };
        return existing.result;
      }
      // Failed domain operations must not leave partially updated data behind.
      const before = structuredClone(draft);
      let result: ToolResult;
      try { result = { ok: true, data: mutate(draft) }; }
      catch (error) {
        Object.assign(draft, before);
        result = { ok: false, error: error instanceof AppError
          ? { code: error.code, message: error.message }
          : { code: "TOOL_ERROR", message: "工具执行失败，未保存修改。" } };
      }
      draft.operations[key] = { fingerprint, result };
      return result;
    });
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.tail;
    await unlink(join(this.home, "instance.lock"));
  }
}
