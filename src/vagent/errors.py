class AppError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def public_error(error: Exception) -> AppError:
    if isinstance(error, AppError):
        return error
    # Provider exceptions can contain request bodies or authorization headers.
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return AppError("AUTH_ERROR", "DeepSeek 凭证无效或没有模型权限。")
    if status == 429:
        return AppError("RATE_LIMIT", "DeepSeek 请求限流，请稍后重试。")
    return AppError("EXECUTION_ERROR", "执行失败，请检查网络、模型配置和本地目录权限。")


def failure(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}
