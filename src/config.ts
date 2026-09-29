import { homedir } from "node:os";
import { resolve } from "node:path";
import { AppError } from "./errors.js";

export function loadConfig(env: NodeJS.ProcessEnv = process.env) {
  return {
    home: resolve(env.VAGENT_HOME || resolve(homedir(), ".vagent")),
    apiKey: env.VAGENT_DEEPSEEK_KEY || env.DEEPSEEK_API_KEY,
    model: env.VAGENT_DEEPSEEK_MODEL || "deepseek-flash",
  };
}

export function requireKey(key: string | undefined): string {
  if (!key?.trim()) throw new AppError("MISSING_KEY", "请配置 DEEPSEEK_API_KEY；无 Key 可运行 demo 查看模拟工具流程。");
  return key.trim();
}

export function assertId(id: string): void {
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(id) || ["__proto__", "constructor", "prototype"].includes(id)) throw new AppError("INVALID_ID", "ID 只能包含 1～64 位字母、数字、下划线或连字符，且不能使用保留名称。");
}
