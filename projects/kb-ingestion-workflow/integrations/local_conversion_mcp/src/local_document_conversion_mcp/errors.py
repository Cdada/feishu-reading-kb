from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, message_zh: str) -> None:
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh


def error_result(error: ServiceError, *, request_id: str) -> dict[str, object]:
    return {
        "schema_version": "local-document-conversion/v1",
        "ok": False,
        "request_id": request_id,
        "task_id": None,
        "status": "failed",
        "error_code": error.code,
        "message_zh": error.message_zh,
        "next_action": None,
    }
