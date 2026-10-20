from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


REQUEST_SCHEMA_VERSION = "kb-conversion.request/v1"
RESULT_SCHEMA_VERSION = "kb-conversion.result/v1"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = PROJECT_ROOT / "schemas"
REQUEST_SCHEMA_PATH = SCHEMA_DIR / "conversion-request-v1.schema.json"
RESULT_SCHEMA_PATH = SCHEMA_DIR / "conversion-result-v1.schema.json"

SELECTED_MODES = {"local-existing", "dify-experimental"}
BACKEND_BY_MODE = {
    "local-existing": "local-existing",
    "dify-experimental": "dify-mineru-api",
}


class ContractError(ValueError):
    pass


class JsonSchemaError(ContractError):
    pass


def validate_conversion_request(data: Any, expected_mode: str | None = None) -> None:
    validate_against_schema(load_request_schema(), data)
    if expected_mode is not None and data.get("selected_mode") != expected_mode:
        raise ContractError(f"request selected_mode must be {expected_mode}")


def validate_conversion_result(data: dict[str, Any]) -> None:
    validate_against_schema(load_result_schema(), data)


def validate_result_matches_request(result: dict[str, Any], request: dict[str, Any]) -> None:
    checks = [
        ("selected_mode", result.get("selected_mode"), request.get("selected_mode")),
        ("task.task_id", result.get("task", {}).get("task_id"), request.get("task_id")),
        ("task.attempt", result.get("task", {}).get("attempt"), request.get("attempt")),
        ("source.filename", result.get("source", {}).get("filename"), request.get("source", {}).get("filename")),
        (
            "source.source_relpath",
            result.get("source", {}).get("source_relpath"),
            request.get("source", {}).get("source_relpath"),
        ),
        ("source.sha256", result.get("source", {}).get("sha256"), request.get("source", {}).get("sha256")),
    ]
    for label, actual, expected in checks:
        if actual != expected:
            raise ContractError(f"result {label} does not match request")


def validate_against_schema(schema: dict[str, Any], value: Any) -> None:
    JsonSchemaValidator(schema).validate(value)


def load_request_schema() -> dict[str, Any]:
    return load_json(REQUEST_SCHEMA_PATH)


def load_result_schema() -> dict[str, Any]:
    return load_json(RESULT_SCHEMA_PATH)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(f"schema must be a JSON object: {path}")
    return data


class JsonSchemaValidator:
    def __init__(self, root_schema: dict[str, Any]) -> None:
        self.root_schema = root_schema

    def validate(self, value: Any, schema: dict[str, Any] | None = None, path: str = "$") -> None:
        active_schema = schema or self.root_schema
        if "$ref" in active_schema:
            self.validate(value, self.resolve_ref(active_schema["$ref"]), path)
            return

        for index, subschema in enumerate(active_schema.get("allOf", [])):
            self.validate(value, subschema, f"{path}.allOf[{index}]")

        if "if" in active_schema:
            try:
                self.validate(value, active_schema["if"], f"{path}.if")
                condition_matched = True
            except JsonSchemaError:
                condition_matched = False
            if condition_matched and "then" in active_schema:
                self.validate(value, active_schema["then"], f"{path}.then")
            if not condition_matched and "else" in active_schema:
                self.validate(value, active_schema["else"], f"{path}.else")

        expected_type = active_schema.get("type")
        if expected_type is not None:
            types = expected_type if isinstance(expected_type, list) else [expected_type]
            if not any(self.type_matches(str(item), value) for item in types):
                raise JsonSchemaError(f"{path}: expected {types}, got {type(value).__name__}")
            if value is None:
                return

        if "const" in active_schema and value != active_schema["const"]:
            raise JsonSchemaError(f"{path}: expected const {active_schema['const']!r}")

        if "enum" in active_schema and value not in active_schema["enum"]:
            raise JsonSchemaError(f"{path}: {value!r} not in enum")

        if isinstance(value, str):
            if len(value) < int(active_schema.get("minLength", 0)):
                raise JsonSchemaError(f"{path}: shorter than minLength")
            if "maxLength" in active_schema and len(value) > int(active_schema["maxLength"]):
                raise JsonSchemaError(f"{path}: longer than maxLength")
            if "pattern" in active_schema and not re.search(str(active_schema["pattern"]), value):
                raise JsonSchemaError(f"{path}: pattern mismatch")

        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in active_schema and value < int(active_schema["minimum"]):
                raise JsonSchemaError(f"{path}: below minimum")

        if isinstance(value, list):
            if "minItems" in active_schema and len(value) < int(active_schema["minItems"]):
                raise JsonSchemaError(f"{path}: fewer than minItems")
            if "maxItems" in active_schema and len(value) > int(active_schema["maxItems"]):
                raise JsonSchemaError(f"{path}: more than maxItems")
            if active_schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
                raise JsonSchemaError(f"{path}: duplicate array items")
            item_schema = active_schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    self.validate(item, item_schema, f"{path}[{index}]")

        if isinstance(value, dict):
            required = active_schema.get("required", [])
            for key in required:
                if key not in value:
                    raise JsonSchemaError(f"{path}: missing required key {key!r}")
            properties = active_schema.get("properties", {})
            if active_schema.get("additionalProperties") is False:
                extra = set(value) - set(properties)
                if extra:
                    raise JsonSchemaError(f"{path}: unexpected keys {sorted(extra)!r}")
            for key, item in value.items():
                if key in properties:
                    self.validate(item, properties[key], f"{path}.{key}")

    def resolve_ref(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/$defs/"):
            raise JsonSchemaError(f"unsupported ref: {ref}")
        name = ref.removeprefix("#/$defs/")
        try:
            target = self.root_schema["$defs"][name]
        except KeyError as exc:
            raise JsonSchemaError(f"missing ref target: {ref}") from exc
        if not isinstance(target, dict):
            raise JsonSchemaError(f"ref target is not an object: {ref}")
        return target

    @staticmethod
    def type_matches(expected: str, value: Any) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "null":
            return value is None
        raise JsonSchemaError(f"unsupported JSON Schema type: {expected}")


def build_failure_result(
    request: Any,
    *,
    stage: str,
    code: str,
    message: str,
    retryable: bool,
    stderr_excerpt: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    request_obj = request if isinstance(request, dict) else {}
    mode = request_obj.get("selected_mode")
    if mode not in SELECTED_MODES:
        mode = "dify-experimental"
    source = request_obj.get("source") if isinstance(request_obj.get("source"), dict) else {}
    parser = request_obj.get("parser") if isinstance(request_obj.get("parser"), dict) else {}
    timestamp = isoformat_z(now or dt.datetime.now(dt.timezone.utc))
    task_id = valid_task_id(request_obj.get("task_id"))
    attempt = (
        request_obj.get("attempt")
        if isinstance(request_obj.get("attempt"), int) and not isinstance(request_obj.get("attempt"), bool)
        else 1
    )
    safe_source = safe_failure_source(source)
    idempotency_material = {
        "task_id": task_id,
        "attempt": attempt,
        "source_sha256": safe_source["sha256"],
        "stage": stage,
        "code": code,
    }
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "failed",
        "selected_mode": mode,
        "task": {
            "task_id": task_id,
            "attempt": max(1, attempt),
            "idempotency_key": "sha256:" + stable_sha256(idempotency_material),
            "started_at": timestamp,
            "finished_at": timestamp,
        },
        "source": safe_source,
        "backend": {
            "name": BACKEND_BY_MODE[mode],
            "workflow_version": None,
            "converter_version": "kb-converter-v1",
            "fallback_used": False,
            "fallback_from": None,
            "fallback_reason": None,
            "warnings": [],
        },
        "parser": {
            "name": "MinerU" if mode == "dify-experimental" else "local-existing",
            "version": None,
            "task_id": None,
            "backend": safe_parser_value(parser.get("backend"), default="pipeline", allowed={"pipeline"}),
            "parse_method": safe_parser_value(parser.get("parse_method"), default="auto", allowed={"auto", "txt", "ocr"}),
        },
        "candidate": None,
        "quality": {
            "schema_valid": False,
            "source_markers_present": False,
            "warnings": [],
        },
        "failure": {
            "stage": stage,
            "code": code,
            "message": message,
            "retryable": retryable,
            "stderr_excerpt": sanitize_excerpt(stderr_excerpt),
        },
    }
    validate_conversion_result(result)
    return result


def safe_failure_source(source: dict[str, Any]) -> dict[str, str]:
    relpath = source.get("source_relpath") if isinstance(source.get("source_relpath"), str) else ""
    sha256 = source.get("sha256") if isinstance(source.get("sha256"), str) else ""
    filename = safe_filename(source.get("filename"))
    if not path_matches_schema(relpath):
        relpath = "00_原始输入/unknown"
    if not sha256_matches_schema(sha256):
        sha256 = "0" * 64
    return {
        "filename": filename,
        "source_relpath": relpath,
        "sha256": sha256,
    }


def valid_task_id(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", value):
        return value
    return "dify-experimental-invalid-request"


def safe_filename(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        return "unknown"
    return value


def safe_parser_value(value: Any, *, default: str, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else default


def path_matches_schema(value: str) -> bool:
    return bool(value) and not value.startswith("/") and "\\" not in value and all(
        part not in {"", ".", ".."} for part in value.split("/")
    )


def sha256_matches_schema(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Fa-f0-9]{64}", value))


def stable_sha256(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", value)
    cleaned = re.sub(r"(?i)(api[_-]?key|authorization)[:=]\S+", r"\1=[REDACTED]", cleaned)
    return cleaned[:4000]


def isoformat_z(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clone_json(data: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(data)
