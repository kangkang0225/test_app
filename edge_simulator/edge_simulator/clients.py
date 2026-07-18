from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any

from .config import BackendSettings, NodeSettings
from .protocol import SIMULATED_JPEG, build_multipart, encode_frame, heartbeat_body, iso_timestamp


LogCallback = Callable[[str, str, str, Any | None], None]
MessageCallback = Callable[["TcpNode", dict[str, Any]], None]


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class BackendHttpClient:
    def __init__(self, settings: BackendSettings, log: LogCallback | None = None):
        self.settings = settings
        self.log = log or (lambda *_: None)

    def request_json(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        token: str | None = None,
        timeout: float | None = None,
        unwrap: bool = True,
    ) -> Any:
        url = path if path.startswith(("http://", "https://")) else f"{self.settings.http_base}{path}"
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        self.log("DEBUG", "HTTP", f"{method.upper()} {path}", payload)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.settings.response_timeout_seconds
            ) as response:
                response_body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            response_body = exc.read()
            parsed = self._parse_body(response_body)
            message = self._error_message(parsed) or f"HTTP {exc.code}"
            raise ApiError(message, status=exc.code, payload=parsed) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"无法访问后端 {url}：{exc}") from exc

        parsed = self._parse_body(response_body)
        if status < 200 or status >= 300:
            raise ApiError(self._error_message(parsed) or f"HTTP {status}", status=status, payload=parsed)
        if isinstance(parsed, dict) and "code" in parsed:
            code = parsed.get("code")
            if code not in (0, 200, "0", "200"):
                raise ApiError(self._error_message(parsed) or f"业务错误 {code}", status=status, payload=parsed)
            return parsed.get("data") if unwrap else parsed
        return parsed

    def upload_camera(
        self,
        device_id: str,
        command_id: int | None,
        *,
        image: bytes = SIMULATED_JPEG,
    ) -> Any:
        fields = {"deviceId": device_id, "exifTimestamp": iso_timestamp()}
        if command_id is not None:
            fields["commandId"] = str(command_id)
        body, content_type = build_multipart(
            fields, "file", f"simulated-{device_id}-{command_id or 'manual'}.jpg", image, "image/jpeg"
        )
        path = "/api/edge/images/upload"
        url = f"{self.settings.http_base}{path}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": content_type},
            method="POST",
        )
        self.log("INFO", device_id, "上传模拟相机图片", {"command_id": command_id})
        try:
            with urllib.request.urlopen(request, timeout=self.settings.response_timeout_seconds) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            parsed = self._parse_body(exc.read())
            raise ApiError(self._error_message(parsed) or f"图片上传失败：HTTP {exc.code}", status=exc.code, payload=parsed) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"图片上传无法访问后端：{exc}") from exc
        parsed = self._parse_body(raw)
        if status < 200 or status >= 300:
            raise ApiError(self._error_message(parsed) or f"图片上传失败：HTTP {status}", status=status, payload=parsed)
        if isinstance(parsed, dict) and parsed.get("code") not in (None, 0, 200, "0", "200"):
            raise ApiError(self._error_message(parsed) or "图片上传业务失败", payload=parsed)
        return parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed

    @staticmethod
    def _parse_body(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("message", "msg", "error"):
                value = payload.get(key)
                if value:
                    return str(value)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        return None


class TcpNode:
    """A reconnectable newline-delimited JSON edge connection."""

    def __init__(
        self,
        backend: BackendSettings,
        settings: NodeSettings,
        *,
        log: LogCallback | None = None,
        on_message: MessageCallback | None = None,
    ):
        self.backend = backend
        self.settings = settings
        self.log = log or (lambda *_: None)
        self.on_message = on_message
        self._socket: socket.socket | None = None
        self._receiver: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._received: deque[tuple[int, dict[str, Any]]] = deque(maxlen=500)
        self._sequence = 0
        self._stop = threading.Event()
        self._connected = threading.Event()

    @property
    def device_id(self) -> str:
        return self.settings.device_id

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def last_sequence(self) -> int:
        with self._condition:
            return self._sequence

    def connect(self, *, wait_for_ack: bool = True) -> dict[str, Any] | None:
        if self.is_connected:
            return None
        if self._socket is not None or self._receiver is not None or self._heartbeat is not None:
            self.close()
        self._stop.clear()
        try:
            sock = socket.create_connection(
                (self.backend.tcp_host, self.backend.tcp_port),
                timeout=self.backend.connect_timeout_seconds,
            )
        except OSError as exc:
            raise ConnectionError(
                f"{self.device_id} 无法连接 TCP {self.backend.tcp_host}:{self.backend.tcp_port}：{exc}"
            ) from exc
        sock.settimeout(0.5)
        self._socket = sock
        self._connected.set()
        self._receiver = threading.Thread(
            target=self._receive_loop, name=f"sim-recv-{self.device_id}", daemon=True
        )
        self._receiver.start()
        self.log("INFO", self.device_id, "TCP 已连接", {"type": self.settings.device_type})
        since = self.last_sequence
        self.send("/api/edge/heartbeat", heartbeat_body(self.device_id))
        if not wait_for_ack:
            self._start_heartbeat_loop()
            return None
        message = self.wait_for_message(
            lambda item: item.get("type") in {"heartbeat_ack", "error"},
            timeout=self.backend.response_timeout_seconds,
            since=since,
        )
        if message is None:
            self.close()
            raise TimeoutError(f"{self.device_id} 心跳响应超时")
        if message.get("type") == "error":
            self.close()
            raise ConnectionError(f"{self.device_id} 心跳被后端拒绝：{message.get('body')}")
        self._start_heartbeat_loop()
        return message

    def close(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        self._connected.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        receiver = self._receiver
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=1.5)
        self._receiver = None
        heartbeat = self._heartbeat
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=1.5)
        self._heartbeat = None

    def send(self, path: str, body: dict[str, Any]) -> None:
        frame = encode_frame(path, body)
        with self._send_lock:
            sock = self._socket
            if sock is None or not self.is_connected:
                raise ConnectionError(f"{self.device_id} 当前离线")
            try:
                sock.sendall(frame)
            except OSError as exc:
                self._connected.clear()
                raise ConnectionError(f"{self.device_id} 发送失败：{exc}") from exc
        self.log("DEBUG", self.device_id, f"上行 {path}", body)

    def request(
        self,
        path: str,
        body: dict[str, Any],
        response_types: set[str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        since = self.last_sequence
        self.send(path, body)
        message = self.wait_for_message(
            lambda item: item.get("type") in response_types | {"error"},
            timeout=timeout or self.backend.response_timeout_seconds,
            since=since,
        )
        if message is None:
            raise TimeoutError(f"{self.device_id} 等待 {', '.join(sorted(response_types))} 超时")
        if message.get("type") == "error":
            raise RuntimeError(f"后端返回错误：{message.get('body')}")
        return message

    def wait_for_message(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float,
        since: int = 0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for sequence, message in self._received:
                    if sequence > since and predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _receive_loop(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.log("WARN", self.device_id, f"TCP 接收中断：{exc}", None)
                break
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("JSON 根节点不是对象")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self.log("WARN", self.device_id, f"忽略无法解析的下行消息：{exc}", raw[:200])
                    continue
                with self._condition:
                    self._sequence += 1
                    self._received.append((self._sequence, message))
                    self._condition.notify_all()
                self.log("DEBUG", self.device_id, f"下行 {message.get('type', 'unknown')}", message.get("body"))
                if self.on_message:
                    try:
                        self.on_message(self, message)
                    except Exception as exc:  # callback failures must not kill receiver
                        self.log("ERROR", self.device_id, f"处理下行消息失败：{exc}", message)
        was_connected = self._connected.is_set()
        self._connected.clear()
        with self._condition:
            self._condition.notify_all()
        if was_connected and not self._stop.is_set():
            self.log("WARN", self.device_id, "TCP 连接已被远端关闭", None)

    def _start_heartbeat_loop(self) -> None:
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name=f"sim-heartbeat-{self.device_id}", daemon=True
        )
        self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.backend.heartbeat_interval_seconds):
            if not self.is_connected:
                return
            try:
                self.send("/api/edge/heartbeat", heartbeat_body(self.device_id))
            except ConnectionError as exc:
                self.log("WARN", self.device_id, f"周期心跳发送失败：{exc}", None)
                return
