from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when local-only service settings are unsafe or incomplete."""


@dataclass(frozen=True)
class SourceRoot:
    name: str
    path: Path


@dataclass(frozen=True)
class ServiceSettings:
    source_roots: dict[str, SourceRoot]
    ledger_path: Path
    output_root: Path
    delivery_root: Path
    minimum_free_bytes: int = 5 * 1024**3
    maximum_source_bytes: int = 2 * 1024**3
    inspection_ttl_seconds: int = 15 * 60
    workflow_script: Path = Path(__file__).resolve().parents[4] / "scripts" / "kb_workflow.py"
    workflow_python: str = sys.executable
    conversion_tool_workspace: Path | None = None
    worker_poll_seconds: float = 2.0
    maximum_delivery_bytes: int = 100 * 1024**2

    def __post_init__(self) -> None:
        allowed_names = {"feishu_inbound", "icloud_inbox", "local_staging"}
        unknown = set(self.source_roots) - allowed_names
        if unknown:
            raise ConfigurationError(f"未知来源类型: {', '.join(sorted(unknown))}")
        if not self.source_roots:
            raise ConfigurationError("至少需要配置一个受控来源目录")
        if self.minimum_free_bytes < 0:
            raise ConfigurationError("minimum_free_bytes 不能为负数")
        if self.maximum_source_bytes <= 0:
            raise ConfigurationError("maximum_source_bytes 必须大于零")
        if self.inspection_ttl_seconds < 60:
            raise ConfigurationError("inspection_ttl_seconds 不能小于 60 秒")
        if self.worker_poll_seconds <= 0:
            raise ConfigurationError("worker_poll_seconds 必须大于零")
        if self.maximum_delivery_bytes <= 0:
            raise ConfigurationError("maximum_delivery_bytes 必须大于零")

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        _reject_cloud_mineru_environment()
        roots: dict[str, SourceRoot] = {}
        root_env = {
            "feishu_inbound": "LOCAL_CONVERSION_FEISHU_INBOUND",
            "icloud_inbox": "LOCAL_CONVERSION_ICLOUD_INBOX",
            "local_staging": "LOCAL_CONVERSION_LOCAL_STAGING",
        }
        for name, env_name in root_env.items():
            value = os.environ.get(env_name, "").strip()
            if value:
                roots[name] = SourceRoot(name=name, path=Path(value))

        ledger = _required_path("LOCAL_CONVERSION_LEDGER")
        output = _required_path("LOCAL_CONVERSION_OUTPUT_ROOT")
        delivery = _required_path("LOCAL_CONVERSION_DELIVERY_ROOT")
        minimum_free = int(os.environ.get("LOCAL_CONVERSION_MIN_FREE_BYTES", str(5 * 1024**3)))
        maximum_source = int(os.environ.get("LOCAL_CONVERSION_MAX_SOURCE_BYTES", str(2 * 1024**3)))
        ttl = int(os.environ.get("LOCAL_CONVERSION_INSPECTION_TTL_SECONDS", "900"))
        workflow_script = Path(
            os.environ.get(
                "LOCAL_CONVERSION_WORKFLOW_SCRIPT",
                str(Path(__file__).resolve().parents[4] / "scripts" / "kb_workflow.py"),
            )
        )
        workflow_python = os.environ.get("LOCAL_CONVERSION_WORKFLOW_PYTHON", sys.executable).strip()
        conversion_workspace = _required_path("LOCAL_CONVERSION_TOOL_WORKSPACE")
        poll_seconds = float(os.environ.get("LOCAL_CONVERSION_WORKER_POLL_SECONDS", "2"))
        maximum_delivery = int(
            os.environ.get("LOCAL_CONVERSION_MAX_DELIVERY_BYTES", str(100 * 1024**2))
        )
        return cls(
            source_roots=roots,
            ledger_path=ledger,
            output_root=output,
            delivery_root=delivery,
            minimum_free_bytes=minimum_free,
            maximum_source_bytes=maximum_source,
            inspection_ttl_seconds=ttl,
            workflow_script=workflow_script,
            workflow_python=workflow_python,
            conversion_tool_workspace=conversion_workspace,
            worker_poll_seconds=poll_seconds,
            maximum_delivery_bytes=maximum_delivery,
        )


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少环境变量 {name}")
    return Path(value)


def load_bearer_token() -> str:
    """Load the MCP bearer secret without accepting an ambiguous source."""
    inline = os.environ.get("LOCAL_CONVERSION_MCP_TOKEN", "")
    token_file_value = os.environ.get("LOCAL_CONVERSION_MCP_TOKEN_FILE", "").strip()
    if inline and token_file_value:
        raise ConfigurationError("MCP Bearer 密钥只能配置一种来源")

    if token_file_value:
        token_path = Path(token_file_value)
        if not token_path.is_absolute():
            raise ConfigurationError("LOCAL_CONVERSION_MCP_TOKEN_FILE 必须是绝对路径")
        try:
            file_stat = token_path.lstat()
        except FileNotFoundError as exc:
            raise ConfigurationError("MCP Bearer 密钥文件不存在") from exc
        if token_path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise ConfigurationError("MCP Bearer 密钥文件必须是非符号链接的普通文件")
        if file_stat.st_uid != os.getuid():
            raise ConfigurationError("MCP Bearer 密钥文件必须由当前用户持有")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ConfigurationError("MCP Bearer 密钥文件权限必须为 0600 或更严格")
        try:
            inline = token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("无法安全读取 MCP Bearer 密钥文件") from exc

    if len(inline) < 32 or len(inline) > 4096 or any(character.isspace() for character in inline):
        raise ConfigurationError("MCP Bearer 密钥必须为 32—4096 个不含空白的字符")
    return inline


def validate_token_file_boundaries(settings: ServiceSettings) -> None:
    token_file_value = os.environ.get("LOCAL_CONVERSION_MCP_TOKEN_FILE", "").strip()
    if not token_file_value:
        return
    try:
        token_path = Path(token_file_value).resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("无法验证 MCP Bearer 密钥文件边界") from exc
    if token_path == settings.ledger_path.resolve(strict=False):
        raise ConfigurationError("MCP Bearer 密钥文件不得与任务账本使用同一路径")
    exposed_roots = [
        *(root.path for root in settings.source_roots.values()),
        settings.output_root,
        settings.delivery_root,
    ]
    for exposed_root in exposed_roots:
        try:
            token_path.relative_to(exposed_root.resolve(strict=False))
        except ValueError:
            continue
        raise ConfigurationError("MCP Bearer 密钥文件不得位于来源、输出或交付目录内")


def _reject_cloud_mineru_environment() -> None:
    for name in ("MINERU_API_TOKEN", "MINERU_TOKEN", "MINERU_API_KEY"):
        if os.environ.get(name, "").strip():
            raise ConfigurationError(f"本机转换服务拒绝继承云端凭据 {name}")

    endpoint = os.environ.get("LOCAL_MINERU_API_URL", "http://127.0.0.1:8000").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError("LOCAL_MINERU_API_URL 必须是本机 HTTP 回环地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("LOCAL_MINERU_API_URL 不得包含凭据、查询参数或片段")
