from __future__ import annotations

import json
import math
import mimetypes
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import ConfigError, SimulatorConfig
from .runtime import SimulatorRuntime


class AttractionController:
    """Owns the UI state machine while the backend remains the source of business truth."""

    def __init__(self, config: SimulatorConfig, runtime: SimulatorRuntime | None = None):
        if not config.attractions:
            raise ConfigError("Web 界面需要配置 attractions；请使用 config.web.example.json")
        self.config = config
        self.runtime = runtime or SimulatorRuntime(config)
        self._lock = threading.RLock()
        self._active_id: str | None = None
        self._entered_at: datetime | None = None
        self._hf_ready = False
        self._hf_checkins: set[str] = set()
        self._transitioning = False
        self._binding_cache = self._default_bindings()
        self._binding_source = "config_default"
        self._binding_error: str | None = None
        self._binding_checked_at = 0.0

    def connect(self) -> dict[str, Any]:
        with self._lock:
            self.runtime.connect_all()
            self.runtime.logs.add("INFO", "WEB", "景点控制台已连接全部模拟节点")
            return self.state()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self.runtime.disconnect_all()
            self._active_id = None
            self._entered_at = None
            self._hf_ready = False
            return self.state()

    def trigger(self, attraction_id: str, tag_name: str) -> dict[str, Any]:
        normalized = tag_name.strip().lower().replace("-", "_")
        if normalized not in {"uhf_b", "uhf_c", "hf", "hf_checkin", "hf_control"}:
            raise ValueError("手动触发仅支持 UHF-B、UHF-C、HF 打卡或 HF 控制确权")
        with self._lock:
            attraction = self.config.attraction(attraction_id)
            if self._active_id != attraction.id:
                raise RuntimeError(f"请先进入 {attraction.name}，UHF-A 会自动建立现场状态")
            protocol_tag = "hf" if normalized.startswith("hf") else normalized
            if protocol_tag not in attraction.tags:
                raise ValueError(f"{attraction.name} 未配置 {protocol_tag.upper().replace('_', '-')}")
            self._require_nodes_online(attraction.id)
            hf_purpose = None
            if protocol_tag == "hf":
                if normalized == "hf":
                    hf_purpose = (
                        "control"
                        if self._has_hf_reader(attraction.id, "control")
                        else "checkin"
                    )
                else:
                    hf_purpose = normalized.removeprefix("hf_")
            self.runtime.send_tag(
                protocol_tag,
                attraction_id=attraction.id,
                hf_purpose=hf_purpose,
                event_time=datetime.now().astimezone(),
            )
            if protocol_tag in {"uhf_b", "uhf_c"}:
                label = protocol_tag.upper().replace("_", "-")
                binding = self._binding_for(protocol_tag)
                target_type = str(binding.get("device_type") or "").lower()
                installed = any(
                    device.attraction_id == attraction.id
                    and device.device_type.lower() == target_type
                    for device in self.config.devices
                )
                if target_type and installed:
                    message = f"已在 {attraction.name} 触发 {label}；当前绑定 {target_type}，本站有对应固定设备"
                elif target_type:
                    message = (
                        f"已在 {attraction.name} 触发 {label}；当前绑定 {target_type}，"
                        "但本站未安装该设备，后端不会凭空创建"
                    )
                else:
                    message = f"已在 {attraction.name} 触发 {label}；App 当前未配置有效绑定"
            elif hf_purpose == "control":
                self._hf_ready = True
                message = f"已在 {attraction.name} 触发 HF 设备控制确权"
            else:
                self._hf_checkins.add(attraction.id)
                message = f"已在 {attraction.name} 完成 HF 景点打卡"
            self.runtime.logs.add("INFO", attraction.name, message)
            return {"ok": True, "message": message, "state": self.state()}

    def control(
        self,
        attraction_id: str,
        device_id: str,
        control_id: str,
        params_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            attraction = self.config.attraction(attraction_id)
            if self._active_id != attraction.id:
                raise RuntimeError(f"请先进入 {attraction.name}")
            if not self._hf_ready:
                raise RuntimeError("请先点击 HF 贴卡完成近场确权")
            device = next(
                (
                    item
                    for item in self.config.devices
                    if item.attraction_id == attraction.id and item.device_id == device_id
                ),
                None,
            )
            if device is None or device.config.get("hf_control") is not True:
                raise ValueError("该设备不是当前景点的 HF 可控设备")
            controls = device.config.get("controls", [])
            spec = next(
                (
                    item
                    for item in controls
                    if isinstance(item, dict) and str(item.get("id")) == control_id
                ),
                None,
            )
            if spec is None:
                raise ValueError(f"设备 {device_id} 未配置控制动作 {control_id}")
            action = str(spec.get("action", "")).strip().lower()
            params = dict(spec.get("params") or {})
            if not action or not isinstance(params, dict):
                raise ConfigError(f"设备 {device_id} 的控制动作配置无效")
            if params_override:
                unexpected = set(params_override) - set(params)
                if unexpected:
                    raise ValueError(f"控制参数不允许：{', '.join(sorted(unexpected))}")
                for key, value in params_override.items():
                    number = int(value)
                    if not 0 <= number <= 100:
                        raise ValueError(f"{key} 必须在 0 到 100 之间")
                    params[key] = number
            result = self.runtime.send_control_command(device.device_id, action, params)
            label = str(spec.get("label") or action)
            message = f"已向 {device.config.get('label') or device.name} 下发：{label}"
            self.runtime.logs.add("INFO", attraction.name, message, result)
            return {"ok": True, "message": message, "command": result, "state": self.state()}

    def reset_journey(self) -> dict[str, Any]:
        with self._lock:
            self._hf_checkins.clear()
            self._hf_ready = False
            self.runtime.logs.add("INFO", "WEB", "已重置测试平台本轮 HF 打卡进度")
            return self.state()

    def toggle(self, attraction_id: str) -> dict[str, Any]:
        with self._lock:
            if self._transitioning:
                raise RuntimeError("另一个景点状态正在切换，请稍后重试")
            self._transitioning = True
            try:
                attraction = self.config.attraction(attraction_id)
                self._require_nodes_online(attraction.id)
                warnings: list[str] = []
                if self._active_id == attraction.id:
                    self._leave(attraction.id)
                    message = f"已离开 {attraction.name}"
                    action = "leave"
                else:
                    if self._active_id:
                        previous = self.config.attraction(self._active_id)
                        self._leave(previous.id)
                        warnings.append(f"已自动离开 {previous.name}")
                    warnings.extend(self._enter(attraction.id))
                    message = f"已进入 {attraction.name}"
                    action = "enter"
            finally:
                self._transitioning = False
            return {
                "ok": True,
                "action": action,
                "message": message,
                "warnings": warnings,
                "state": self.state(),
            }

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_bindings()
            rows = self.runtime.status_rows()
            captures = self.runtime.capture_records()
            online_count = sum(row["status"] == "online" for row in rows)
            node_by_attraction: dict[str, list[dict[str, str]]] = {}
            for row in rows:
                node_by_attraction.setdefault(row["attraction"], []).append(row)
            attractions = []
            for attraction in self.config.attractions:
                nodes = node_by_attraction.get(attraction.id, [])
                devices = [
                    {
                        "device_id": node.device_id,
                        "name": node.config.get("label") or node.name,
                        "type": node.device_type.lower(),
                        "status": next(
                            (
                                row["status"]
                                for row in nodes
                                if row["device_id"] == node.device_id
                            ),
                            "offline",
                        ),
                        "hf_control": node.config.get("hf_control") is True,
                        "controls": [
                            {
                                "id": str(control.get("id")),
                                "label": str(control.get("label") or control.get("action")),
                                "action": str(control.get("action")),
                                "params": dict(control.get("params") or {}),
                            }
                            for control in node.config.get("controls", [])
                            if isinstance(control, dict) and control.get("id") and control.get("action")
                        ],
                    }
                    for node in self.config.devices
                    if node.attraction_id == attraction.id
                ]
                interaction_bindings = []
                for tag in ("uhf_b", "uhf_c"):
                    if tag not in attraction.tags:
                        continue
                    binding = self._binding_for(tag)
                    device_type = str(binding.get("device_type") or "").lower()
                    matching = [device for device in devices if device["type"] == device_type]
                    interaction_bindings.append(
                        {
                            "tag": tag,
                            "tag_label": tag.upper().replace("_", "-"),
                            "device_type": device_type or None,
                            "action": binding.get("action"),
                            "configured": bool(binding.get("configured", device_type)),
                            "installed": bool(matching),
                            "available": any(device["status"] == "online" for device in matching),
                            "device_name": matching[0]["name"] if matching else None,
                        }
                    )
                hf_purposes = {
                    reader.hf_purpose
                    for reader in self.config.readers
                    if reader.attraction_id == attraction.id and reader.device_type == "HF"
                }
                camera = next((device for device in devices if device["type"] == "camera"), None)
                capture = captures.get(camera["device_id"]) if camera else None
                required_online = bool(nodes) and all(row["status"] == "online" for row in nodes)
                attractions.append(
                    {
                        "id": attraction.id,
                        "order": len(attractions) + 1,
                        "name": attraction.name,
                        "district": attraction.district,
                        "description": attraction.description,
                        "tags": list(attraction.tags),
                        "devices": devices,
                        "interaction_bindings": interaction_bindings,
                        "accent": attraction.accent,
                        "dwell_seconds": attraction.simulated_dwell_seconds,
                        "camera_installed": camera is not None,
                        "capture": (
                            {
                                **capture,
                                "image_url": f"/media/captures/{camera['device_id']}?v={capture['command_id']}",
                            }
                            if capture and camera
                            else None
                        ),
                        "inside": self._active_id == attraction.id,
                        "hf_ready": self._active_id == attraction.id and self._hf_ready,
                        "hf_checkin_available": "checkin" in hf_purposes,
                        "hf_control_available": "control" in hf_purposes,
                        "checked_in": attraction.id in self._hf_checkins,
                        "ready": required_online,
                    }
                )
            logs = [
                {
                    "timestamp": entry.timestamp,
                    "level": entry.level,
                    "source": entry.source,
                    "message": entry.message,
                }
                for entry in reversed(self.runtime.logs.recent(80))
                if entry.level != "DEBUG"
            ]
            total_spots = len(self.config.attractions)
            required_spots = math.ceil(total_spots * 0.8)
            completed_spots = len(self._hf_checkins)
            return {
                "ui": {
                    "eyebrow": self.config.ui.eyebrow,
                    "title": self.config.ui.title,
                    "description": self.config.ui.description,
                    "guide_title": self.config.ui.guide_title,
                    "guide_image_url": "/media/guide" if self.config.ui.guide_image_path else None,
                },
                "connected": bool(rows) and online_count == len(rows),
                "online_count": online_count,
                "node_count": len(rows),
                "active_attraction_id": self._active_id,
                "entered_at": self._entered_at.isoformat() if self._entered_at else None,
                "transitioning": self._transitioning,
                "bindings": list(self._binding_cache),
                "binding_source": self._binding_source,
                "binding_error": self._binding_error,
                "journey": {
                    "completed_spots": completed_spots,
                    "total_spots": total_spots,
                    "required_spots": required_spots,
                    "threshold_percent": 80,
                    "progress_percent": (
                        round(completed_spots / total_spots * 100, 1) if total_spots else 0
                    ),
                    "qualified": completed_spots >= required_spots,
                    "completed_attraction_ids": sorted(self._hf_checkins),
                    "source": "simulator_session",
                },
                "attractions": attractions,
                "nodes": rows,
                "logs": logs,
            }

    def close(self) -> None:
        self.runtime.close()

    def _enter(self, attraction_id: str) -> list[str]:
        attraction = self.config.attraction(attraction_id)
        now = datetime.now().astimezone()
        self.runtime.simulate_dwell(
            attraction.simulated_dwell_seconds,
            attraction_id=attraction.id,
            rssi=-42,
        )
        self._active_id = attraction.id
        self._entered_at = now
        self._hf_ready = False
        self.runtime.logs.add(
            "INFO",
            attraction.name,
            f"进入景点；UHF-A 以加速时间模拟已停留 {attraction.simulated_dwell_seconds:g} 秒",
        )
        return []

    def _leave(self, attraction_id: str) -> None:
        attraction = self.config.attraction(attraction_id)
        self.runtime.send_tag(
            "uhf_a",
            attraction_id=attraction.id,
            event_time=datetime.now().astimezone(),
            rssi=-46,
            event_type="leave",
        )
        self.runtime.logs.add("INFO", attraction.name, "离开景点；已上报 UHF-A 明确离场事件")
        self._active_id = None
        self._entered_at = None
        self._hf_ready = False

    def _require_nodes_online(self, attraction_id: str) -> None:
        nodes = [row for row in self.runtime.status_rows() if row["attraction"] == attraction_id]
        offline = [row["device_id"] for row in nodes if row["status"] != "online"]
        if not nodes or offline:
            detail = "、".join(offline) if offline else "未配置节点"
            raise ConnectionError(f"该景点设备尚未就绪：{detail}；请先点击“连接全部设备”")

    def _has_hf_reader(self, attraction_id: str, purpose: str) -> bool:
        return any(
            reader.device_type == "HF"
            and reader.attraction_id == attraction_id
            and reader.hf_purpose == purpose
            for reader in self.config.readers
        )

    def _default_bindings(self) -> list[dict[str, Any]]:
        return [
            {
                "tag": str(binding["tag"]),
                "tag_type": str(binding["tag"]).upper().replace("_", "-"),
                "device_type": str(binding["device_type"]).lower(),
                "action": str(binding["action"]).lower(),
                "configured": True,
                "active": bool(binding.get("is_active", True)),
            }
            for binding in self.config.bindings
            if binding.get("tag") in {"uhf_b", "uhf_c"}
        ]

    def _binding_for(self, tag: str) -> dict[str, Any]:
        return next((item for item in self._binding_cache if item.get("tag") == tag), {})

    def _refresh_bindings(self) -> None:
        now = time.monotonic()
        if now - self._binding_checked_at < 3:
            return
        self._binding_checked_at = now
        try:
            raw_bindings = self.runtime.interaction_bindings()
            normalized: list[dict[str, Any]] = []
            for item in raw_bindings:
                tag_type = str(item.get("tagType") or item.get("tag_type") or "")
                tag = tag_type.strip().lower().replace("-", "_")
                if tag not in {"uhf_b", "uhf_c"}:
                    continue
                device_type = item.get("deviceType") or item.get("device_type")
                normalized.append(
                    {
                        "id": item.get("id"),
                        "tag": tag,
                        "tag_type": tag_type,
                        "tag_label": item.get("tagLabel") or item.get("tag_label"),
                        "device_type": str(device_type).lower() if device_type else None,
                        "action": item.get("action"),
                        "configured": bool(item.get("configured", device_type is not None)),
                        "active": bool(item.get("active", True)),
                    }
                )
            if normalized:
                self._binding_cache = normalized
                self._binding_source = "app_backend"
                self._binding_error = None
            else:
                self._binding_error = "当前腕带没有 UHF-B/UHF-C 绑定"
        except Exception as exc:
            self._binding_cache = self._default_bindings()
            self._binding_source = "config_default"
            self._binding_error = str(exc)


class SimulatorWebServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR can let multiple simulator processes bind the
    # same port and serve a random mix of stale and current state/assets.
    allow_reuse_address = False


def create_server(
    config: SimulatorConfig,
    host: str,
    port: int,
    *,
    runtime: SimulatorRuntime | None = None,
) -> tuple[SimulatorWebServer, AttractionController]:
    controller = AttractionController(config, runtime)
    assets = Path(__file__).with_name("web_assets")
    media: dict[str, Path] = {}
    if config.ui.guide_image_path:
        media["/media/guide"] = config.ui.guide_image_path
    for device in config.devices:
        if device.device_type == "CAMERA" and device.image_path:
            media[f"/media/captures/{device.device_id}"] = device.image_path

    class Handler(BaseHTTPRequestHandler):
        server_version = "RfidSimulatorUI/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self._security_headers()
                self.end_headers()
                return
            if path == "/api/state":
                self._json(HTTPStatus.OK, {"ok": True, "data": controller.state()})
                return
            media_path = media.get(path)
            if media_path is not None:
                try:
                    content = media_path.read_bytes()
                except OSError as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self._security_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            asset_map = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            asset = asset_map.get(path)
            if asset is None:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "页面不存在"})
                return
            file_path = assets / asset[0]
            try:
                content = file_path.read_bytes()
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", asset[1])
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            path = unquote(urlsplit(self.path).path)
            try:
                if path == "/api/connect":
                    result = controller.connect()
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                    return
                if path == "/api/disconnect":
                    result = controller.disconnect()
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                    return
                if path == "/api/journey/reset":
                    result = controller.reset_journey()
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[:2] == ["api", "attractions"] and parts[3] == "tags":
                    result = controller.trigger(parts[2], parts[4])
                    self._json(HTTPStatus.OK, result)
                    return
                if len(parts) == 6 and parts[:2] == ["api", "attractions"] and parts[3] == "controls":
                    payload = self._read_json_body()
                    params = payload.get("params") if isinstance(payload, dict) else None
                    if params is not None and not isinstance(params, dict):
                        raise ValueError("params 必须是对象")
                    result = controller.control(parts[2], parts[4], parts[5], params)
                    self._json(HTTPStatus.OK, result)
                    return
                prefix = "/api/attractions/"
                suffix = "/toggle"
                if path.startswith(prefix) and path.endswith(suffix):
                    attraction_id = path[len(prefix) : -len(suffix)].strip("/")
                    result = controller.toggle(attraction_id)
                    self._json(HTTPStatus.OK, result)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
            except (ConfigError, ConnectionError, KeyError, RuntimeError, TimeoutError, ValueError) as exc:
                controller.runtime.logs.add("ERROR", "WEB", str(exc))
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc), "data": controller.state()})
            except Exception as exc:
                controller.runtime.logs.add("ERROR", "WEB", f"未预期错误：{exc}")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length 无效") from exc
            if length <= 0:
                return {}
            if length > 64 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("请求体必须是 UTF-8 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'",
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    try:
        server = SimulatorWebServer((host, port), Handler)
    except Exception:
        controller.close()
        raise
    return server, controller


def serve_web(
    config: SimulatorConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    verbose: bool = False,
) -> None:
    runtime = SimulatorRuntime(config, verbose=verbose)
    server, controller = create_server(config, host, port, runtime=runtime)
    url = f"http://{host}:{server.server_port}/"
    print(f"成都景点腕带模拟界面已启动：{url}", flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.shutdown()
        server.server_close()
        controller.close()
