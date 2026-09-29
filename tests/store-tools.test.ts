import { writeFile, readFile } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createProjectTools } from "../src/agent/tools.js";
import { FileStore } from "../src/storage/store.js";
import { testStore } from "./helpers.js";

describe("project persistence and tool boundaries", () => {
  it("preserves versions, rejects stale updates and prevents cross-project access", async () => {
    const store = await testStore();
    await store.ensureSession("coffee");
    await store.ensureSession("other");
    const tools = createProjectTools();
    const call = (name: string, args: unknown, operationKey: string, projectId = "coffee") => tools.execute(name, args, { store, projectId, operationKey });
    const first = await call("artifact_save", { kind: "brief", title: "暖色", content: "原始内容" }, "save-1");
    expect(first.ok).toBe(true);
    const artifactId = Object.keys(store.snapshot().artifacts)[0]!;
    await call("artifact_save", { artifactId, expectedVersion: 1, kind: "brief", title: "雨夜", content: "更新内容" }, "save-2");
    expect(await call("artifact_save", { artifactId, expectedVersion: 1, kind: "brief", title: "覆盖", content: "错误内容" }, "save-3")).toMatchObject({ ok: false, error: { code: "VERSION_CONFLICT" } });
    expect(await call("artifact_read", { artifactId }, "read-other", "other")).toMatchObject({ ok: false, error: { code: "NOT_FOUND" } });
    expect(await call("artifact_read", { artifactId, version: 1 }, "read-v1")).toMatchObject({ ok: true, data: { content: "原始内容" } });
    expect(store.snapshot().artifacts[artifactId]!.versions).toHaveLength(2);
    await store.close();
    const reopened = await FileStore.open(store.home);
    try { expect(reopened.snapshot().artifacts[artifactId]!.versions).toHaveLength(2); }
    finally { await reopened.close(); }
  });

  it("commits operation results with writes and reuses a saved result after reopening", async () => {
    const store = await testStore();
    await store.ensureSession("default");
    const args = { kind: "script", title: "脚本", content: "正文" };
    const tools = createProjectTools();
    const first = await tools.execute("artifact_save", args, { store, projectId: "default", operationKey: "durable-operation" });
    await store.close();
    const reopened = await FileStore.open(store.home);
    try {
      const repeated = await tools.execute("artifact_save", args, { store: reopened, projectId: "default", operationKey: "durable-operation" });
      expect(repeated).toEqual(first);
      expect(Object.keys(reopened.snapshot().artifacts)).toHaveLength(1);
      expect(await tools.execute("artifact_save", { ...args, content: "changed" }, { store: reopened, projectId: "default", operationKey: "durable-operation" })).toMatchObject({ ok: false, error: { code: "OPERATION_CONFLICT" } });
    } finally { await reopened.close(); }
  });

  it("does not overwrite a corrupt store and releases the failed startup lock", async () => {
    const store = await testStore();
    await store.close();
    await writeFile(join(store.home, "state.json"), "broken-json");
    await expect(FileStore.open(store.home)).rejects.toMatchObject({ code: "INVALID_STORE" });
    expect(await readFile(join(store.home, "state.json"), "utf8")).toBe("broken-json");
    await expect(FileStore.open(store.home)).rejects.toMatchObject({ code: "INVALID_STORE" });
  });

  it("rejects a second writer and prototype/path IDs", async () => {
    const store = await testStore();
    await expect(FileStore.open(store.home)).rejects.toMatchObject({ code: "STORE_LOCKED" });
    await expect(store.ensureSession("../../outside")).rejects.toMatchObject({ code: "INVALID_ID" });
    await expect(store.ensureSession("__proto__")).rejects.toMatchObject({ code: "INVALID_ID" });
  });

  it("rolls back a failing domain operation", async () => {
    const store = await testStore();
    await store.ensureSession("default");
    const result = await store.operation("failure", "test", {}, (state) => { state.projects.default!.goal = "should roll back"; throw new Error("failed"); });
    expect(result.ok).toBe(false);
    expect(store.snapshot().projects.default!.goal).toBe("");
  });
});
