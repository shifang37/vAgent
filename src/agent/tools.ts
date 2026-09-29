import { randomUUID } from "node:crypto";
import { z } from "zod";
import { AppError } from "../errors.js";
import type { Database, FileStore, ToolResult } from "../storage/store.js";

export interface ToolSpec { name: string; description: string; schema: z.ZodType }
export interface ToolDefinition extends ToolSpec {
  effect: "read" | "write";
  execute: (args: unknown, state: Database, projectId: string) => unknown;
}

export class ToolRegistry {
  private readonly definitions = new Map<string, ToolDefinition>();
  register<T extends z.ZodType>(definition: Omit<ToolDefinition, "schema" | "execute"> & {
    schema: T;
    execute: (args: z.infer<T>, state: Database, projectId: string) => unknown;
  }): this {
    if (this.definitions.has(definition.name)) throw new Error(`Duplicate tool: ${definition.name}`);
    this.definitions.set(definition.name, definition as ToolDefinition);
    return this;
  }
  specs(): ToolSpec[] { return [...this.definitions.values()].map(({ name, description, schema }) => ({ name, description, schema })); }
  async execute(name: string, args: unknown, context: { store: FileStore; projectId: string; operationKey: string }): Promise<ToolResult> {
    const definition = this.definitions.get(name);
    if (!definition) return { ok: false, error: { code: "UNKNOWN_TOOL", message: `工具 ${name.slice(0, 80)} 未注册。` } };
    const parsed = definition.schema.safeParse(args);
    if (!parsed.success) return { ok: false, error: { code: "INVALID_ARGUMENTS", message: parsed.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`).join("; ").slice(0, 1500) } };
    return context.store.operation(context.operationKey, name, parsed.data, (draft) => definition.execute(parsed.data, draft, context.projectId));
  }
}

export function createProjectTools(): ToolRegistry {
  return new ToolRegistry()
    .register({ name: "project_read", effect: "read", description: "读取当前创作项目、版本和产物列表。用户修改需求前应读取现有状态。", schema: z.object({}).strict(),
      execute: (_, state, id) => ({ ...state.projects[id], artifacts: Object.values(state.artifacts).filter((a) => a.projectId === id).map((a) => ({ id: a.id, kind: a.kind, ...a.versions.at(-1), content: undefined })) }),
    })
    .register({ name: "project_update", effect: "write", description: "修改当前项目的明确创作需求；使用 project_read 返回的 revision 防止覆盖较新的状态。未传字段保持原值。",
      schema: z.object({ expectedRevision: z.number().int().nonnegative(), goal: z.string().max(2000).optional(), audience: z.string().max(500).optional(), style: z.string().max(500).optional(), constraints: z.array(z.string().max(500)).max(20).optional() }).strict(),
      execute: ({ expectedRevision, ...updates }, state, id) => {
        const project = state.projects[id]!;
        if (project.revision !== expectedRevision) throw new AppError("REVISION_CONFLICT", "项目版本已变化，请重新读取后再修改。");
        Object.assign(project, updates);
        project.revision += 1;
        return project;
      },
    })
    .register({ name: "plan_update", effect: "write", description: "保存复杂创作任务的简短执行清单；简单问题无需调用。",
      schema: z.object({ steps: z.array(z.object({ text: z.string().min(1).max(200), status: z.enum(["pending", "in_progress", "completed"]) }).strict()).max(12) }).strict(),
      execute: ({ steps }, state, id) => { state.projects[id]!.plan = steps; state.projects[id]!.revision += 1; return { steps }; },
    })
    .register({ name: "artifact_save", effect: "write", description: "真实保存创作方案、脚本或文本分镜。新增时省略 artifactId；修改时同时传 artifactId 和 expectedVersion，保留原版。",
      schema: z.object({ artifactId: z.string().uuid().optional(), expectedVersion: z.number().int().positive().optional(), kind: z.enum(["brief", "script", "storyboard"]), title: z.string().min(1).max(120), content: z.string().min(1).max(20000) }).strict().refine((a) => Boolean(a.artifactId) === Boolean(a.expectedVersion), "修改时必须同时提供 artifactId 和 expectedVersion"),
      execute: (args, state, id) => {
        const artifactId = args.artifactId ?? randomUUID();
        const existing = state.artifacts[artifactId];
        if (args.artifactId && (!existing || existing.projectId !== id)) throw new AppError("NOT_FOUND", "当前项目中没有该产物。");
        if (existing && (existing.versions.at(-1)!.version !== args.expectedVersion || existing.kind !== args.kind)) throw new AppError("VERSION_CONFLICT", "产物版本或类型不匹配，请重新读取。");
        const artifact = existing ?? { id: artifactId, projectId: id, kind: args.kind, versions: [] };
        const version = artifact.versions.length + 1;
        artifact.versions.push({ version, title: args.title, content: args.content, createdAt: new Date().toISOString() });
        state.artifacts[artifactId] = artifact;
        return { artifactId, version, title: args.title, persisted: true };
      },
    })
    .register({ name: "artifact_read", effect: "read", description: "按 ID 读取当前项目的产物。可以指定历史版本，长内容按 offset/limit 分段读取。",
      schema: z.object({ artifactId: z.string().uuid(), version: z.number().int().positive().optional(), offset: z.number().int().nonnegative().default(0), limit: z.number().int().min(1).max(8000).default(4000) }).strict(),
      execute: ({ artifactId, version, offset, limit }, state, id) => {
        const artifact = state.artifacts[artifactId];
        if (!artifact || artifact.projectId !== id) throw new AppError("NOT_FOUND", "当前项目中没有该产物。");
        const selected = version ? artifact.versions.find((v) => v.version === version) : artifact.versions.at(-1);
        if (!selected) throw new AppError("NOT_FOUND", "没有该版本。");
        return { artifactId, ...selected, content: selected.content.slice(offset, offset + limit), totalCharacters: selected.content.length, nextOffset: offset + limit < selected.content.length ? offset + limit : null };
      },
    });
}
