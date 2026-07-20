from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .clients import ApiError, BackendHttpClient, TcpNode
from .config import ConfigError, NodeSettings, SimulatorConfig
from .protocol import command_ack_body, create_app_jwt, event_batch_body
from .protocol import SIMULATED_JPEG


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    level: str
    source: str
    message: str
    data: Any = None


class LogBook:
    def __init__(self, *, verbose: bool = False, capacity: int = 1000):
        self.verbose = verbose
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, level: str, source: str, message: str, data: Any = None) -> None:
        entry = LogEntry(
            timestamp=datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3],
            level=level.upper(),
            source=source,
            message=message,
            data=data,
        )
        with self._lock:
            self._entries.append(entry)
        if entry.level != "DEBUG" or self.verbose:
            detail = ""
            if data is not None and self.verbose:
                detail = " " + json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
            print(f"[{entry.timestamp}] {entry.level:<5} {entry.source}: {entry.message}{detail}", flush=True)

    def recent(self, limit: int = 30) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)[-max(1, limit) :]


class SimulatorRuntime:
    TAG_FIELDS = {
        "uhf_a": "uhf_a_uid",
        "uhf_b": "uhf_b_uid",
        "uhf_c": "uhf_c_uid",
        "hf": "hf_uid",
    }

    def __init__(self, config: SimulatorConfig, *, verbose: bool = False):
        self.config = config
        self.logs = LogBook(verbose=verbose)
        self.http = BackendHttpClient(config.backend, self.logs.add)
        self.readers = {
            item.name: TcpNode(config.backend, item, log=self.logs.add, on_message=self._on_message)
            for item in config.readers
        }
        self.devices = {
            item.name: TcpNode(config.backend, item, log=self.logs.add, on_message=self._on_message)
            for item in config.devices
        }
        self._nodes_by_device_id = {
            node.device_id: node for node in [*self.readers.values(), *self.devices.values()]
        }
        self._ack_modes = {item.device_id: item.auto_ack for item in config.devices}
        self._command_threads: set[threading.Thread] = set()
        self._command_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._captures: dict[str, dict[str, Any]] = {}

    def connect_all(self) -> None:
        connected: list[TcpNode] = []
        try:
            # 控制设备必须先在线，随后腕带事件触发的下行命令才有接收方。
            for node in [*self.devices.values(), *self.readers.values()]:
                was_connected = node.is_connected
                node.connect()
                if not was_connected:
                    connected.append(node)
        except Exception:
            for node in reversed(connected):
                node.close()
            raise

    def disconnect_all(self) -> None:
        for node in [*self.readers.values(), *self.devices.values()]:
            node.close()
        self.logs.add("INFO", "SYSTEM", "所有模拟节点已断开")

    def close(self) -> None:
        self._wait_for_command_handlers()
        for node in [*self.readers.values(), *self.devices.values()]:
            node.close()
        with self._command_lock:
            threads = list(self._command_threads)
        for thread in threads:
            thread.join(timeout=0.2)

    def reconnect(self, name_or_id: str) -> None:
        node = self._find_node(name_or_id)
        if node.is_connected:
            self.logs.add("INFO", node.device_id, "已经在线")
            return
        node.connect()

    def disconnect(self, name_or_id: str) -> None:
        node = self._find_node(name_or_id)
        node.close()
        self.logs.add("INFO", node.device_id, "已模拟离线")

    def set_ack_mode(self, name_or_id: str, mode: str) -> None:
        node = self._find_device(name_or_id)
        normalized = mode.lower()
        if normalized not in {"success", "failed", "timeout", "rejected", "none"}:
            raise ValueError("ACK 模式仅支持 success/failed/timeout/rejected/none")
        self._ack_modes[node.device_id] = normalized
        self.logs.add("INFO", node.device_id, f"ACK 模式已改为 {normalized}")

    def send_tag(
        self,
        tag_name: str,
        *,
        rssi: int = -45,
        attraction_id: str | None = None,
        hf_purpose: str | None = None,
        event_time: datetime | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_tag_name(tag_name)
        reader_type = "HF" if normalized == "hf" else "UHF"
        if hf_purpose is not None and normalized != "hf":
            raise ValueError("只有 HF 标签可以指定 hf_purpose")
        current = event_time or datetime.now().astimezone()
        return self._send_events(
            normalized,
            [(current, rssi)],
            reader_type,
            attraction_id=attraction_id,
            hf_purpose=hf_purpose,
            event_type=event_type,
        )

    def simulate_dwell(
        self,
        seconds: int | float = 35,
        *,
        rssi: int = -42,
        attraction_id: str | None = None,
    ) -> dict[str, Any]:
        dwell_seconds = float(seconds)
        if dwell_seconds <= 0:
            raise ValueError("停留秒数必须大于 0")
        now = datetime.now().astimezone()
        self.logs.add(
            "INFO",
            "SCENARIO",
            f"模拟 UHF-A 景点停留 {dwell_seconds:g} 秒（使用历史事件时间，无需实际等待）",
        )
        return self._send_events(
            "uhf_a",
            [(now - timedelta(seconds=dwell_seconds), rssi), (now, rssi)],
            "UHF",
            attraction_id=attraction_id,
        )

    def _send_events(
        self,
        tag_name: str,
        samples: list[tuple[datetime, int | None]],
        reader_type: str,
        *,
        attraction_id: str | None = None,
        hf_purpose: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        reader_config = self.config.reader_for_type(
            reader_type,
            attraction_id,
            hf_purpose=hf_purpose,
        )
        reader = self.readers[reader_config.name]
        if not reader.is_connected:
            raise ConnectionError(f"读写器 {reader.device_id} 离线")
        tag_uid = str(self.config.wristband[self.TAG_FIELDS[tag_name]])
        body = event_batch_body(
            reader.device_id,
            [(tag_uid, event_time, rssi) for event_time, rssi in samples],
            event_type=event_type,
        )
        response = reader.request(
            "/api/edge/event-batches", body, {"batch_ack"}, timeout=self.config.backend.response_timeout_seconds
        )
        self.logs.add(
            "INFO",
            reader.device_id,
            f"腕带标签 {tag_name.upper()} 已上报"
            f"（{len(samples)} 条事件{f'，{event_type}' if event_type else ''}）",
            response.get("body"),
        )
        return response

    def app_token(self) -> str | None:
        token = os.environ.get(self.config.app.token_env, "").strip()
        if token:
            return token
        secret = os.environ.get(self.config.app.jwt_secret_env, "")
        if secret and self.config.app.user_id is not None:
            return create_app_jwt(
                secret,
                self.config.app.user_id,
                algorithm=self.config.app.jwt_algorithm,
                ttl_seconds=self.config.app.token_ttl_seconds,
            )
        return None

    def current_control(self) -> dict[str, Any]:
        token = self.app_token()
        if not token:
            raise ConfigError(
                f"需要环境变量 {self.config.app.token_env}，或同时配置 app.user_id 与环境变量 {self.config.app.jwt_secret_env}"
            )
        result = self.http.request_json("GET", "/api/app/control/current", token=token)
        if not isinstance(result, dict):
            raise ApiError("当前控制权限接口返回格式异常", payload=result)
        return result

    def interaction_bindings(self) -> list[dict[str, Any]]:
        """Read the App's current UHF-B/UHF-C choices without mutating them."""
        token = self.app_token()
        if not token:
            raise ConfigError(f"缺少 App JWT：请设置 {self.config.app.token_env}")
        result = self.http.request_json(
            "GET",
            "/api/app/interaction-bindings",
            token=token,
        )
        if not isinstance(result, list):
            raise ApiError("互动绑定接口返回格式异常", payload=result)
        return [dict(item) for item in result if isinstance(item, dict)]

    def capture_records(self) -> dict[str, dict[str, Any]]:
        with self._capture_lock:
            return {device_id: dict(record) for device_id, record in self._captures.items()}

    def send_control_command(
        self,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        control_token: str | None = None,
    ) -> dict[str, Any]:
        token = self.app_token()
        if not token:
            raise ConfigError(f"缺少 App JWT：请设置 {self.config.app.token_env}")
        current = None
        if not control_token:
            current = self.current_control()
            control_token = current.get("control_token") or current.get("controlToken")
        if not control_token:
            reason = current.get("reason") if isinstance(current, dict) else None
            raise RuntimeError(f"当前没有可用的 HF 控制令牌{f'：{reason}' if reason else ''}")
        result = self.http.request_json(
            "POST",
            "/api/app/control/commands",
            {
                "control_token": control_token,
                "device_id": device_id,
                "action": action,
                "params": params or {},
            },
            token=token,
        )
        if not isinstance(result, dict):
            raise ApiError("控制命令接口返回格式异常", payload=result)
        self.logs.add("INFO", "APP", f"已提交控制命令：{device_id} {action}", result)
        return result

    def upload_camera_image(self, name_or_id: str, command_id: int) -> Any:
        node = self._find_device(name_or_id)
        if node.settings.device_type != "CAMERA":
            raise ValueError(f"{node.device_id} 不是 camera 类型设备")
        result = self.http.upload_camera(
            node.device_id,
            int(command_id),
            image=self._camera_image(node.settings),
        )
        self.logs.add("INFO", node.device_id, f"命令 #{command_id} 的模拟图片手动上传成功", result)
        return result

    def run_default_scenario(self) -> None:
        dwell = float(self.config.scenario.get("dwell_seconds", 35))
        delay = max(0.0, float(self.config.scenario.get("step_delay_ms", 400)) / 1000)
        self.logs.add("INFO", "SCENARIO", "开始完整腕带交互场景")
        self.simulate_dwell(dwell)
        time.sleep(delay)
        self.send_tag("uhf_b")
        time.sleep(delay)
        self.send_tag("uhf_c")
        time.sleep(delay)
        self.send_tag("hf")
        time.sleep(delay)

        hf_command = self.config.scenario.get("hf_command", {})
        if isinstance(hf_command, dict) and hf_command.get("enabled", False):
            device_id = str(hf_command.get("device_id", "")).strip()
            action = str(hf_command.get("action", "")).strip()
            if not device_id or not action:
                raise ConfigError("scenario.hf_command 启用后必须填写 device_id 与 action")
            self.send_control_command(device_id, action, hf_command.get("params") or {})
            time.sleep(delay)
        self._wait_for_command_handlers()
        self.logs.add("INFO", "SCENARIO", "完整场景执行完毕")

    def status_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for role, nodes in (("reader", self.readers), ("device", self.devices)):
            for node in nodes.values():
                rows.append(
                    {
                        "name": node.settings.name,
                        "device_id": node.device_id,
                        "role": role,
                        "type": node.settings.device_type,
                        "attraction": node.settings.attraction_id or "-",
                        "status": "online" if node.is_connected else "offline",
                        "ack": self._ack_modes.get(node.device_id, "-") if role == "device" else "-",
                    }
                )
        return rows

    def _on_message(self, node: TcpNode, message: dict[str, Any]) -> None:
        if message.get("type") != "command":
            return
        body = message.get("body")
        if not isinstance(body, dict):
            self.logs.add("ERROR", node.device_id, "下行 command 的 body 不是对象", message)
            return
        target = str(body.get("device_id", ""))
        if target and target != node.device_id:
            self.logs.add("WARN", node.device_id, f"收到发给其他设备的命令：{target}", body)
            return
        thread = threading.Thread(
            target=self._handle_command,
            args=(node, body),
            name=f"sim-command-{node.device_id}-{body.get('command_id')}",
            daemon=True,
        )
        with self._command_lock:
            self._command_threads.add(thread)
        thread.start()

    def _handle_command(self, node: TcpNode, body: dict[str, Any]) -> None:
        try:
            command_id = int(body["command_id"])
            command_type = str(body.get("command_type", "UHF")).upper()
            action = str(body.get("action", ""))
            mode = self._ack_modes.get(node.device_id, "success")
            self.logs.add(
                "INFO", node.device_id, f"收到 {command_type} 命令 #{command_id}：{action}", body
            )
            if mode == "none":
                self.logs.add("WARN", node.device_id, f"按配置忽略命令 #{command_id}，用于测试 ACK 超时")
                return
            time.sleep(max(0, node.settings.ack_delay_ms) / 1000)
            ack = command_ack_body(
                node.device_id,
                command_id,
                mode,
                command_type=command_type,
                error_code=None if mode == "success" else f"SIMULATED_{mode.upper()}",
                error_message=None if mode == "success" else f"模拟设备返回 {mode}",
            )
            node.request(
                "/api/edge/command-ack",
                ack,
                {"command_ack"},
                timeout=self.config.backend.response_timeout_seconds,
            )
            self.logs.add("INFO", node.device_id, f"命令 #{command_id} 已回传 ACK：{mode}")
            if mode == "success" and node.settings.auto_upload and node.settings.device_type == "CAMERA":
                if command_type != "UHF":
                    self.logs.add(
                        "WARN",
                        node.device_id,
                        f"跳过命令 #{command_id} 的图片上传：现有后端只关联 UHF 命令记录",
                    )
                else:
                    result = self.http.upload_camera(
                        node.device_id,
                        command_id,
                        image=self._camera_image(node.settings),
                    )
                    with self._capture_lock:
                        self._captures[node.device_id] = {
                            "device_id": node.device_id,
                            "command_id": command_id,
                            "captured_at": datetime.now().astimezone().isoformat(),
                            "file_name": (
                                node.settings.image_path.name
                                if node.settings.image_path is not None
                                else f"simulated-{node.device_id}.jpg"
                            ),
                        }
                    self.logs.add("INFO", node.device_id, f"命令 #{command_id} 的模拟图片上传成功", result)
        except Exception as exc:
            self.logs.add("ERROR", node.device_id, f"处理设备命令失败：{exc}", body)
        finally:
            current = threading.current_thread()
            with self._command_lock:
                self._command_threads.discard(current)

    def _wait_for_command_handlers(self) -> None:
        deadline = time.monotonic() + self.config.backend.response_timeout_seconds + 2
        while time.monotonic() < deadline:
            with self._command_lock:
                threads = list(self._command_threads)
            if not threads:
                return
            for thread in threads:
                thread.join(timeout=0.1)

    def _camera_image(self, settings: NodeSettings) -> bytes:
        if settings.image_path is None:
            return SIMULATED_JPEG
        try:
            image = settings.image_path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"无法读取相机预埋照片 {settings.image_path}：{exc}") from exc
        if not image:
            raise ConfigError(f"相机预埋照片为空：{settings.image_path}")
        self.logs.add(
            "INFO",
            settings.device_id,
            f"使用预埋照片模拟拍摄：{settings.image_path.name}",
        )
        return image

    def _find_node(self, name_or_id: str) -> TcpNode:
        key = name_or_id.lower()
        node = self.readers.get(key) or self.devices.get(key) or self._nodes_by_device_id.get(name_or_id)
        if not node:
            raise KeyError(f"找不到节点：{name_or_id}")
        return node

    def _find_device(self, name_or_id: str) -> TcpNode:
        key = name_or_id.lower()
        node = self.devices.get(key) or self._nodes_by_device_id.get(name_or_id)
        if not node or node not in self.devices.values():
            raise KeyError(f"找不到控制设备：{name_or_id}")
        return node

    @classmethod
    def _normalize_tag_name(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        aliases = {"a": "uhf_a", "b": "uhf_b", "c": "uhf_c", "uhfa": "uhf_a", "uhfb": "uhf_b", "uhfc": "uhf_c"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in cls.TAG_FIELDS:
            raise KeyError(f"未知标签：{value}；支持 uhf-a/uhf-b/uhf-c/hf")
        return normalized
