"""Offline Feishu-shaped intake adapter. No SDK, credentials or network client."""
from contextlib import contextmanager
import io
import os

from .contracts import ContractError, ENTRY_SCHEMA, new_id, stable_key, validate_entry
from .staging import BLOCK, IntegrityError, write_all


class MockAttachment:
    def __init__(self, content, available=True):
        self.content, self.available, self.download_calls = content, available, 0

    @contextmanager
    def download(self):
        self.download_calls += 1
        if not self.available:
            raise OSError("mock Feishu attachment unavailable")
        with io.BytesIO(self.content) as stream:
            yield stream


class FeishuMockIntake:
    def __init__(self, registry, staging, worker=None):
        self.registry, self.staging, self.worker = registry, staging, worker

    def receive_message(self, message, resource_index, attachment, group=None):
        return self.receive(normalized_message_contract(message, resource_index, group), attachment)

    def receive(self, contract, attachment):
        row = self.registry.register(contract)

        @contextmanager
        def inbound_source():
            path = self.registry.runtime.checked(self.registry.runtime.path('inbound') / (new_id("transport") + ".bin"))
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with attachment.download() as stream:
                    total = 0
                    while True:
                        chunk = stream.read(BLOCK)
                        if not isinstance(chunk, bytes):
                            raise IntegrityError("transport must yield bytes")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.registry.runtime.config["max_asset_bytes"]:
                            raise IntegrityError("transport exceeds DEV size limit")
                        write_all(fd, chunk)
                    os.fsync(fd)
            finally:
                os.close(fd)
            with os.fdopen(self.registry.runtime.read_fd(path), "rb") as source:
                yield source

        try:
            return self.staging.ingest(row["entry_id"], inbound_source)
        finally:
            # Notification errors are stored by Worker; they never undo accepted.
            # Only this explicit execution path dispatches; status queries do not.
            if self.worker is not None:
                self.worker.drain()


def normalized_message_contract(message, resource_index, group=None):
    """Map the existing gateway's normalized camelCase envelope, offline only.

    gateway.mjs / document-reference.mjs supply messageId, resourceMessageId,
    chatId and resources[{type,fileKey,fileName}]. No platform API is called.
    """
    message_id, chat_id = message.get("messageId"), message.get("chatId")
    if not isinstance(message_id, str) or not message_id or not isinstance(chat_id, str) or not chat_id:
        raise ContractError("mock gateway envelope requires messageId and chatId")
    resources = message.get("resources")
    if not isinstance(resources, list) or type(resource_index) is not int or not 0 <= resource_index < len(resources):
        raise ContractError("invalid resource index")
    resource = resources[resource_index]
    if resource.get("type") not in ("file", "image"):
        raise ContractError("unsupported mock attachment type")
    # A missing received name must be resolved by the intake adapter explicitly;
    # do not pretend an invented file name came from the platform.
    received_name = resource.get("fileName")
    if not isinstance(received_name, str) or not received_name:
        raise ContractError("explicit received filename required for this S4 adapter")
    contract = {
        "schema_version": ENTRY_SCHEMA,
        "intake_key": stable_key("feishu-dev", chat_id, message_id),
        "entry_key": stable_key(message.get("resourceMessageId") or message_id,
                                resource.get("fileKey"), resource_index),
        "received_filename": received_name,
        "lineage": {"message_id": message_id, "resource_message_id": message.get("resourceMessageId"),
                    "file_key": resource.get("fileKey"), "attachment_id": resource.get("attachmentId"),
                    "chat_id": chat_id, "message_at": message.get("messageAt")},
    }
    if resource.get("sizeBytes") is not None:
        contract["expected"] = {"size_bytes": resource["sizeBytes"]}
    if resource.get("sha256") is not None:
        # Trusted live adapters can bind approved bytes before acceptance.
        # validate_entry validates the hash shape; Staging verifies the bytes.
        contract.setdefault("expected", {})["sha256"] = resource["sha256"]
    if group is not None:
        contract["group"] = group
    return validate_entry(contract)
