from __future__ import annotations

import json
import math
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_BASE_URL = "http://127.0.0.1"
DEFAULT_USER = "codex-kb-ingestion"
DEFAULT_TIMEOUT_SECONDS = 30.0


class DifyConfigurationError(RuntimeError):
    pass


class DifyApiError(RuntimeError):
    pass


class NoRedirectHTTPRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class DifyClientConfig:
    base_url: str
    api_key: str
    user: str = DEFAULT_USER
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DifyClientConfig":
        source = env if env is not None else os.environ
        api_key = source.get("DIFY_API_KEY", "").strip()
        if not api_key:
            raise DifyConfigurationError("DIFY_API_KEY is not set")
        base_url = source.get("DIFY_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
        validate_loopback_base_url(base_url)
        try:
            timeout_seconds = float(source.get("DIFY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        except ValueError as exc:
            raise DifyConfigurationError("DIFY_TIMEOUT_SECONDS must be numeric") from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 600:
            raise DifyConfigurationError("DIFY_TIMEOUT_SECONDS must be finite, positive, and no more than 600")
        return cls(
            base_url=base_url,
            api_key=api_key,
            user=source.get("DIFY_WORKFLOW_USER", DEFAULT_USER).strip() or DEFAULT_USER,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DifyApiError(f"Dify response is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DifyApiError("Dify response JSON must be an object")
        return data


class DifyHttpTransport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        ...

    def post_multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        timeout_seconds: float,
    ) -> HttpResponse:
        ...


class UrllibDifyTransport:
    def __init__(self, opener: Any | None = None) -> None:
        self.opener = opener or urllib.request.build_opener(NoRedirectHTTPRedirectHandler)

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        return self._open(request, timeout_seconds)

    def post_multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        timeout_seconds: float,
    ) -> HttpResponse:
        boundary = f"----kb-ingestion-{uuid.uuid4().hex}"
        body = encode_multipart(boundary, fields, files)
        request = urllib.request.Request(
            url,
            data=body,
            headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._open(request, timeout_seconds)

    def _open(self, request: urllib.request.Request, timeout_seconds: float) -> HttpResponse:
        try:
            with self.opener.open(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                body=exc.read(),
                headers=dict(exc.headers.items()),
            )


class DifyClient:
    def __init__(
        self,
        config: DifyClientConfig,
        transport: DifyHttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibDifyTransport()

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        transport: DifyHttpTransport | None = None,
    ) -> "DifyClient":
        return cls(DifyClientConfig.from_env(env), transport=transport)

    def upload_file(self, path: Path) -> str:
        data = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = self.transport.post_multipart(
            f"{self.config.base_url}/v1/files/upload",
            headers=self.auth_headers(),
            fields={"user": self.config.user},
            files={"file": (path.name, data, media_type)},
            timeout_seconds=self.config.timeout_seconds,
        )
        payload = self.require_success(response)
        upload_id = payload.get("id")
        if not isinstance(upload_id, str) or not upload_id:
            raise DifyApiError("Dify upload response missing id")
        return upload_id

    def run_workflow(self, inputs: dict[str, Any]) -> dict[str, Any]:
        response = self.transport.post_json(
            f"{self.config.base_url}/v1/workflows/run",
            headers=self.auth_headers(),
            payload={
                "inputs": inputs,
                "response_mode": "blocking",
                "user": self.config.user,
            },
            timeout_seconds=self.config.timeout_seconds,
        )
        return self.require_success(response)

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"}

    @staticmethod
    def require_success(response: HttpResponse) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            excerpt = response.body.decode("utf-8", errors="replace")[:500]
            raise DifyApiError(f"Dify API returned HTTP {response.status_code}: {excerpt}")
        return response.json()


def validate_loopback_base_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise DifyConfigurationError("DIFY_BASE_URL must use http or https")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DifyConfigurationError("DIFY_BASE_URL must contain only scheme, host, and optional port")
    try:
        parsed.port
    except ValueError as exc:
        raise DifyConfigurationError("DIFY_BASE_URL has an invalid port") from exc
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise DifyConfigurationError("DIFY_BASE_URL must point to localhost, 127.0.0.1, or ::1")


def encode_multipart(
    boundary: str,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, data, media_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {media_type}\r\n\r\n".encode("utf-8"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)
