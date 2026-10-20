"""Versioned, deliberately small S1 contracts; never upgrade V1 implicitly."""
import hashlib
import json
import re
import uuid

SCHEMA = "registry/v2.1"
ENTRY_SCHEMA = "workflow-intake/v2.1"
EVENT_VERSION = "custody-event/v2.1"
TEMPLATE_VERSION = "custody-notice/v2.1"
SUCCESS = "原始文件已安全进入待归档区，飞书附件可按需清理。"
GROUP_SUCCESS = "本组必需原始文件已安全进入待归档区，相关飞书附件可按需清理。"
FAILURE = "原始文件暂未完成本地接管，请先保留飞书附件。"
LINEAGE_FIELDS = ("message_id", "resource_message_id", "file_key", "attachment_id",
                  "chat_id", "message_at")


class ContractError(ValueError):
    pass


def new_id(kind):
    return f"{kind}_{uuid.uuid4().hex}"


def stable_key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(kind + r"_[0-9a-f]{32}", value):
        raise ContractError(f"invalid {kind} id")
    return value


def read_version(document):
    """Return a route, not a modified document. Unversioned legacy stays V1."""
    version = document.get("schema_version")
    if version is None or version in ("kb-source-group/v1", "registry/v1"):
        return "v1_read_only"
    if version in (SCHEMA, ENTRY_SCHEMA, "kb-source-group/v2.1"):
        return "v2.1"
    raise ContractError("unsupported schema_version")


def validate_entry(data):
    required = {"schema_version", "intake_key", "entry_key", "received_filename"}
    optional = {"lineage", "expected", "group"}
    if set(data) - required - optional or not required <= set(data):
        raise ContractError("unknown or missing entry fields")
    if data["schema_version"] != ENTRY_SCHEMA:
        raise ContractError("intake requires explicit workflow-intake/v2.1")
    for name in ("intake_key", "entry_key", "received_filename"):
        if not isinstance(data[name], str) or not data[name] or len(data[name]) > 2048:
            raise ContractError(f"invalid {name}")
    lineage = data.get("lineage", {})
    if not isinstance(lineage, dict) or set(lineage) - set(LINEAGE_FIELDS):
        raise ContractError("invalid lineage fields")
    if any(v is not None and not isinstance(v, str) for v in lineage.values()):
        raise ContractError("lineage values must be text or null")
    expected = data.get("expected", {})
    if not isinstance(expected, dict) or set(expected) - {"size_bytes", "sha256", "md5"}:
        raise ContractError("invalid expected evidence")
    if "size_bytes" in expected and (type(expected["size_bytes"]) is not int or expected["size_bytes"] < 0):
        raise ContractError("invalid expected size")
    for name, length in (("sha256", 64), ("md5", 32)):
        if name in expected and not re.fullmatch(f"[0-9a-f]{{{length}}}", str(expected[name])):
            raise ContractError(f"invalid expected {name}")
    group = data.get("group")
    if group is not None:
        if set(group) != {"source_group_id", "membership_revision", "required"}:
            raise ContractError("invalid group fields")
        validate_id(group["source_group_id"], "group")
        if type(group["membership_revision"]) is not int or group["membership_revision"] < 1:
            raise ContractError("invalid membership revision")
        if type(group["required"]) is not bool:
            raise ContractError("required must be boolean")
    return data


def safe_filename(value):
    # Preserve the supplied name in SQLite; never interpret it as a path.
    result = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", value).strip(" .")
    while ".." in result:
        result = result.replace("..", "_")
    return (result.encode("utf-8")[:160].decode("utf-8", errors="ignore") or "unnamed")
