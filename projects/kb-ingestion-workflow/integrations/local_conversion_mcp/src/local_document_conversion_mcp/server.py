from __future__ import annotations

import hmac
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Literal

from .config import (
    ConfigurationError,
    ServiceSettings,
    load_bearer_token,
    validate_token_file_boundaries,
)
from .errors import ServiceError, error_result
from .service import ConversionService


ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]],
    Awaitable[None],
]
LOGGER = logging.getLogger("local_document_conversion_mcp")
SourceKind = Literal["feishu_inbound", "icloud_inbox", "local_staging"]
DuplicateAction = Literal["not_applicable", "reuse_existing", "convert_as_new_version", "cancel"]
DuplicateConfirmationAction = Literal["reuse_existing", "convert_as_new_version", "cancel"]
DocumentLanguage = Literal[
    "ch", "ch_server", "ch_lite", "en", "korean", "japan", "chinese_cht",
    "ta", "te", "ka", "th", "el", "latin", "arabic", "east_slavic", "cyrillic", "devanagari",
]
DocumentProfile = Literal["general_text", "digital_text", "scanned_text"]


def redact_sensitive_text(value: str, sensitive_values: tuple[str, ...] = ()) -> str:
    redacted = value
    for sensitive in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        redacted = redacted.replace(sensitive, "<redacted>")
    redacted = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", redacted)
    redacted = re.sub(
        r"(?i)\b(authorization|token|secret|password)\s*([=:])\s*[^\s,;]+",
        r"\1\2<redacted>",
        redacted,
    )
    return redacted


class SensitiveLogFilter(logging.Filter):
    def __init__(self, sensitive_values: tuple[str, ...]) -> None:
        super().__init__()
        self.sensitive_values = sensitive_values

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage(), self.sensitive_values)
        record.args = ()
        return True


def install_log_redaction(sensitive_values: tuple[str, ...]) -> None:
    for item in LOGGER.filters:
        if isinstance(item, SensitiveLogFilter):
            item.sensitive_values = sensitive_values
            return
    LOGGER.addFilter(SensitiveLogFilter(sensitive_values))


class StaticBearerMiddleware:
    """Minimal ASGI bearer guard without Starlette BaseHTTPMiddleware."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if len(token) < 32:
            raise ConfigurationError("MCP Bearer 密钥至少需要 32 个字符")
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope.get("type") == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send({"type": "http.response.body", "body": "Unauthorized".encode("utf-8")})
                return
            if scope.get("path") == "/healthz":
                if scope.get("method") != "GET":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 405,
                            "headers": [(b"allow", b"GET"), (b"content-length", b"0")],
                        }
                    )
                    await send({"type": "http.response.body", "body": b""})
                    return
                body = json.dumps(
                    {"status": "ok", "service": "local-document-conversion"},
                    separators=(",", ":"),
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_server(service: ConversionService) -> Any:
    from mcp.server import MCPServer

    mcp = MCPServer(
        "local-document-conversion",
        instructions=(
            "本服务只处理受控本机投递目录中的文件，第一阶段只生成可入库候选资料包，"
            "不改写正文、不写正式知识库。飞书附件先用 source_kind=feishu_inbound 和提示中"
            " .dsh-im/inbound/ 后的相对路径调用 inspect_submission；若返回 confirm_duplicate，"
            "必须把匹配文档名称和位置告诉用户并等待其选择。确认后调用"
            " confirm_duplicate_submission，由服务重新检查并原子提交；不要从历史中抄写"
            " inspection_id。没有重复时才调用 submit_conversion，"
            "再用 wait_task_update 展示进度；终态后依次读取 get_quality_report、"
            "list_result_files。需要回传时调用 prepare_delivery，随后把返回的"
            " outbound_tool_path 交给 dsh_im_return_file。不得用 shell 绕过本服务的来源、"
            "重复确认、质量门禁或交付边界。用户说‘直接执行’时，submit_conversion 使用"
            " document_language=ch、document_profile=general_text；用户明确说英文时用 en，"
            "日文时用 japan。不得声称使用了未由工具结果确认的语言或解析参数。"
        ),
    )

    def guarded(call: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        request_id = f"req_error_{os.urandom(8).hex()}"
        try:
            return call(**kwargs)
        except ServiceError as exc:
            return error_result(exc, request_id=request_id)
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            LOGGER.error("tool_failed request_id=%s error_type=%s", request_id, type(exc).__name__)
            return error_result(
                ServiceError("INTERNAL_ERROR", "本机转换服务发生内部错误，请凭参考号联系管理员"),
                request_id=request_id,
            )

    @mcp.tool()
    def inspect_submission(source_kind: SourceKind, source_relpath: str, topic: str | None = None) -> dict[str, Any]:
        """检查受控投递文件，不启动转换。飞书附件使用 feishu_inbound，并取提示中 .dsh-im/inbound/ 后的相对路径；若返回 confirm_duplicate，必须先向用户确认。"""
        return guarded(
            service.inspect_submission,
            source_kind=source_kind,
            source_relpath=source_relpath,
            topic=topic,
        )

    @mcp.tool()
    def submit_conversion(
        inspection_id: str,
        duplicate_action: DuplicateAction,
        requested_by: str,
        idempotency_key: str,
        document_language: DocumentLanguage = "ch",
        document_profile: DocumentProfile = "general_text",
    ) -> dict[str, Any]:
        """根据检查记录创建异步任务。直接执行默认 document_language=ch、document_profile=general_text；英文用 en，日文用 japan。duplicate_action 必须直接取 inspect_submission 返回值。工具会回显实际转换参数，提交后用 wait_task_update 展示进度。"""
        return guarded(
            service.submit_conversion,
            inspection_id=inspection_id,
            duplicate_action=duplicate_action,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            document_language=document_language,
            document_profile=document_profile,
        )

    @mcp.tool()
    def confirm_duplicate_submission(
        source_kind: SourceKind,
        source_relpath: str,
        duplicate_action: DuplicateConfirmationAction,
        requested_by: str,
        idempotency_key: str,
        document_language: DocumentLanguage = "ch",
        document_profile: DocumentProfile = "general_text",
    ) -> dict[str, Any]:
        """用户确认重复处理选择后使用。服务会重新检查同一受控来源，并在同一次调用内原子提交，调用方不得从历史消息抄写 inspection_id。"""
        return guarded(
            service.confirm_duplicate_submission,
            source_kind=source_kind,
            source_relpath=source_relpath,
            duplicate_action=duplicate_action,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            document_language=document_language,
            document_profile=document_profile,
        )

    @mcp.tool()
    def get_task_status(task_id: str) -> dict[str, Any]:
        """查询本机转换任务状态。"""
        return guarded(service.get_task_status, task_id=task_id)

    @mcp.tool()
    def wait_task_update(
        task_id: str,
        after_updated_at: str | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        """等待任务阶段变化，适合飞书连续展示进度；最多等待 55 秒，不需要 shell。"""
        return guarded(
            service.wait_task_update,
            task_id=task_id,
            after_updated_at=after_updated_at,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    def cancel_task(task_id: str, reason_zh: str | None = None) -> dict[str, Any]:
        """取消允许取消的任务；不会删除原文件或历史结果。"""
        return guarded(service.cancel_task, task_id=task_id, reason_zh=reason_zh)

    @mcp.tool()
    def get_quality_report(task_id: str) -> dict[str, Any]:
        """读取终态任务的结构化质量结论；不会改写或美化失败结论。"""
        return guarded(service.get_quality_report, task_id=task_id)

    @mcp.tool()
    def list_result_files(task_id: str) -> dict[str, Any]:
        """列出候选资料包及质量报告中的允许成果，不列出私有日志或中间缓存。"""
        return guarded(service.list_result_files, task_id=task_id)

    @mcp.tool()
    def prepare_delivery(task_id: str, delivery_mode: str = "recommended") -> dict[str, Any]:
        """把允许成果复制或打包到飞书交付目录；返回 outbound_tool_path 后，应调用 dsh_im_return_file 将成果发给当前飞书用户。"""
        return guarded(
            service.prepare_delivery,
            task_id=task_id,
            delivery_mode=delivery_mode,
        )

    return mcp


def main() -> None:
    from mcp.server.transport_security import TransportSecuritySettings
    import uvicorn

    settings = ServiceSettings.from_env()
    token = load_bearer_token()
    validate_token_file_boundaries(settings)
    host = "127.0.0.1"
    port = int(os.environ.get("LOCAL_CONVERSION_MCP_PORT", "18766"))
    service = ConversionService(settings)
    private_values = (
        token,
        *(str(root.path) for root in settings.source_roots.values()),
        str(settings.ledger_path),
        str(settings.output_root),
        str(settings.delivery_root),
        os.environ.get("LOCAL_CONVERSION_MCP_TOKEN_FILE", ""),
    )
    install_log_redaction(private_values)
    if service.recovered_task_count:
        LOGGER.warning("recovered_interrupted_tasks count=%d", service.recovered_task_count)
    mcp = build_server(service)
    security = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    app = mcp.streamable_http_app(
        host=host,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    uvicorn.run(
        StaticBearerMiddleware(app, token),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
