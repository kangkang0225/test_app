from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Iterable


def iso_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    return current.isoformat(timespec="milliseconds")


def encode_frame(path: str, body: dict[str, Any]) -> bytes:
    payload = {"path": path, "body": body}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def heartbeat_body(device_id: str, sent_at: datetime | None = None) -> dict[str, Any]:
    return {"device_id": device_id, "sent_at": iso_timestamp(sent_at)}


def event_batch_body(
    reader_id: str,
    tag_events: Iterable[tuple[str, datetime, int | None]],
    *,
    batch_id: str | None = None,
    sent_at: datetime | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    normalized_event_type = event_type.lower() if event_type else None
    if normalized_event_type not in {None, "seen", "leave"}:
        raise ValueError(f"不支持的在场事件类型：{event_type}")
    events = []
    for tag_id, event_time, rssi in tag_events:
        event: dict[str, Any] = {"tag_id": tag_id, "event_time": iso_timestamp(event_time)}
        if rssi is not None:
            event["rssi_strength"] = int(rssi)
        if normalized_event_type:
            event["event_type"] = normalized_event_type
        events.append(event)
    if not events:
        raise ValueError("事件批次不能为空")
    if len(events) > 500:
        raise ValueError("单个批次最多包含 500 条事件")
    return {
        "batch_id": batch_id or str(uuid.uuid4()),
        "reader_id": reader_id,
        "sent_at": iso_timestamp(sent_at),
        "events": events,
    }


def command_ack_body(
    device_id: str,
    command_id: int,
    status: str,
    *,
    command_type: str = "UHF",
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    normalized = status.lower()
    if normalized not in {"success", "failed", "timeout", "rejected"}:
        raise ValueError(f"不支持的 ACK 状态：{status}")
    body: dict[str, Any] = {
        "command_type": command_type.upper(),
        "device_id": device_id,
        "command_id": int(command_id),
        "sent_at": iso_timestamp(),
        "status": normalized,
    }
    if error_code:
        body["error_code"] = error_code
    if error_message:
        body["error_message"] = error_message
    return body


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_app_jwt(
    secret: str,
    user_id: int,
    *,
    algorithm: str = "auto",
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    secret_bytes = secret.encode("utf-8")
    selected = algorithm.upper()
    if selected == "AUTO":
        if len(secret_bytes) >= 64:
            selected = "HS512"
        elif len(secret_bytes) >= 48:
            selected = "HS384"
        else:
            selected = "HS256"
    digest_map = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}
    digest = digest_map.get(selected)
    if digest is None:
        raise ValueError(f"不支持的 JWT 算法：{algorithm}")
    if len(secret_bytes) < {"HS256": 32, "HS384": 48, "HS512": 64}[selected]:
        raise ValueError(f"{selected} 密钥长度不足")
    issued_at = int(now if now is not None else time.time())
    header = {"alg": selected, "typ": "JWT"}
    payload = {
        "sub": str(int(user_id)),
        "user_id": int(user_id),
        "token_type": "app",
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
    }
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(secret_bytes, signing_input.encode("ascii"), digest).digest()
    return f"{signing_input}.{_b64url(signature)}"


def build_multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----RfidSimulator{secrets.token_hex(12)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# 合法的 1x1 像素 JPEG；相机设备可用它完成端到端上传测试。
SIMULATED_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
    "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
)
