export class AppError extends Error {
  constructor(public readonly code: string, message: string) {
    super(message);
    this.name = "AppError";
  }
}

export function publicError(error: unknown): AppError {
  if (error instanceof AppError) return error;
  // Provider errors may echo headers or request bodies. Never print them verbatim.
  const status = (error as { status?: number } | null)?.status;
  if (status === 401 || status === 403) return new AppError("AUTH_ERROR", "DeepSeek 凭证无效或没有模型权限。");
  if (status === 429) return new AppError("RATE_LIMIT", "DeepSeek 请求限流，请稍后重试。");
  return new AppError("EXECUTION_ERROR", "执行失败，请检查网络、模型配置和本地目录权限。");
}
