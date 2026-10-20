from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from .client import DifyApiError, DifyClient, DifyConfigurationError
from .result_validator import (
    ContractError,
    build_failure_result,
    clone_json,
    validate_conversion_request,
    validate_conversion_result,
    validate_result_matches_request,
)


MAX_DIFY_UPLOAD_BYTES = 15 * 1024 * 1024


class SourceFileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def execute_dify_experimental(
    request: Any,
    *,
    source_file: Path | None = None,
    env: dict[str, str] | None = None,
    client: DifyClient | None = None,
) -> dict[str, Any]:
    try:
        validate_conversion_request(request, expected_mode="dify-experimental")
    except ContractError as exc:
        return build_failure_result(
            request,
            stage="input_validation",
            code="INPUT_UNSUPPORTED",
            message=str(exc),
            retryable=False,
        )

    try:
        validate_source_file_for_request(request, source_file)
    except SourceFileError as exc:
        return build_failure_result(
            request,
            stage="input_validation",
            code=exc.code,
            message=str(exc),
            retryable=False,
        )

    try:
        active_client = client or DifyClient.from_env(env)
        upload_file_id = active_client.upload_file(source_file)
        workflow_response = active_client.run_workflow(build_workflow_inputs(request, upload_file_id))
        result = extract_conversion_result(workflow_response)
        validate_conversion_result(result)
        validate_result_matches_request(result, request)
        return result
    except (DifyConfigurationError, DifyApiError, OSError) as exc:
        return build_failure_result(
            request,
            stage="dify_run",
            code="DIFY_API_UNAVAILABLE",
            message="Dify API is unavailable.",
            retryable=True,
            stderr_excerpt=str(exc),
        )
    except (ContractError, ValueError, TypeError) as exc:
        return build_failure_result(
            request,
            stage="result_validation",
            code="OUTPUT_SCHEMA_INVALID",
            message=f"Dify result did not satisfy conversion-result v1: {exc}",
            retryable=False,
        )


def build_workflow_inputs(request: dict[str, Any], upload_file_id: str | None) -> dict[str, Any]:
    inputs = {
        "conversion_request": clone_json(request),
    }
    if upload_file_id:
        inputs["source_file"] = {
            "type": "document",
            "transfer_method": "local_file",
            "upload_file_id": upload_file_id,
        }
    return inputs


def validate_source_file_for_request(request: dict[str, Any], source_file: Path | None) -> None:
    if source_file is None:
        raise SourceFileError("INPUT_UNSUPPORTED", "source_file is required for dify-experimental")
    if not source_file.is_file():
        raise SourceFileError("INPUT_UNSUPPORTED", "source_file must be an existing regular file")

    source = request["source"]
    if source_file.name != source["filename"]:
        raise SourceFileError("INPUT_UNSUPPORTED", "source_file name does not match request source.filename")

    actual_size = source_file.stat().st_size
    if actual_size > MAX_DIFY_UPLOAD_BYTES:
        raise SourceFileError("DIFY_UPLOAD_TOO_LARGE", "source_file exceeds the 15 MiB Dify upload limit")
    if actual_size != source["size_bytes"]:
        raise SourceFileError("INPUT_UNSUPPORTED", "source_file size does not match request source.size_bytes")

    actual_sha256 = sha256_file(source_file)
    if actual_sha256.lower() != source["sha256"].lower():
        raise SourceFileError("INPUT_UNSUPPORTED", "source_file SHA-256 does not match request source.sha256")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_conversion_result(workflow_response: dict[str, Any]) -> dict[str, Any]:
    if workflow_response.get("schema_version") == "kb-conversion.result/v1":
        return workflow_response

    outputs = workflow_response.get("data", {}).get("outputs") if isinstance(workflow_response.get("data"), dict) else None
    if not isinstance(outputs, dict):
        raise ValueError("Dify workflow response missing data.outputs")

    for key in ("conversion_result", "result"):
        value = outputs.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
    raise ValueError("Dify workflow outputs did not contain a conversion result object")
