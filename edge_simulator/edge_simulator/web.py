from __future__ import annotations

import json
import threading
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
        self._transitioning = False

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
            rows = self.runtime.status_rows()
            online_count = sum(row["status"] == "online" for row in rows)
            node_by_attraction: dict[str, list[dict[str, str]]] = {}
            for row in rows:
                node_by_attraction.setdefault(row["attraction"], []).append(row)
            attractions = []
            for attraction in self.config.attractions:
                nodes = node_by_attraction.get(attraction.id, [])
                devices = [
                    {
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
                    }
                    for node in self.config.devices
                    if node.attraction_id == attraction.id
                ]
                required_online = bool(nodes) and all(row["status"] == "online" for row in nodes)
                attractions.append(
                    {
                        "id": attraction.id,
                        "name": attraction.name,
                        "district": attraction.district,
                        "description": attraction.description,
                        "tags": list(attraction.tags),
                        "devices": devices,
                        "accent": attraction.accent,
                        "dwell_seconds": attraction.simulated_dwell_seconds,
                        "inside": self._active_id == attraction.id,
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
            return {
                "connected": bool(rows) and online_count == len(rows),
                "online_count": online_count,
                "node_count": len(rows),
                "active_attraction_id": self._active_id,
                "entered_at": self._entered_at.isoformat() if self._entered_at else None,
                "transitioning": self._transitioning,
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
        self.runtime.logs.add(
            "INFO",
            attraction.name,
            f"进入景点；UHF-A 以加速时间模拟已停留 {attraction.simulated_dwell_seconds:g} 秒",
        )
        warnings: list[str] = []
        for tag in attraction.tags:
            if tag == "uhf_a":
                continue
            try:
                self.runtime.send_tag(tag, attraction_id=attraction.id, event_time=now)
            except Exception as exc:
                warning = f"{tag.upper()} 交互失败：{exc}"
                warnings.append(warning)
                self.runtime.logs.add("WARN", attraction.name, warning)
        return warnings

    def _leave(self, attraction_id: str) -> None:
        attraction = self.config.attraction(attraction_id)
        self.runtime.send_tag(
            "uhf_a", attraction_id=attraction.id, event_time=datetime.now().astimezone(), rssi=-46
        )
        self.runtime.logs.add("INFO", attraction.name, "离开景点；已上报末次 UHF-A 感知")
        self._active_id = None
        self._entered_at = None

    def _require_nodes_online(self, attraction_id: str) -> None:
        nodes = [row for row in self.runtime.status_rows() if row["attraction"] == attraction_id]
        offline = [row["device_id"] for row in nodes if row["status"] != "online"]
        if not nodes or offline:
            detail = "、".join(offline) if offline else "未配置节点"
            raise ConnectionError(f"该景点设备尚未就绪：{detail}；请先点击“连接全部设备”")


class SimulatorWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    config: SimulatorConfig,
    host: str,
    port: int,
    *,
    runtime: SimulatorRuntime | None = None,
) -> tuple[SimulatorWebServer, AttractionController]:
    controller = AttractionController(config, runtime)
    assets = Path(__file__).with_name("web_assets")

    class Handler(BaseHTTPRequestHandler):
        server_version = "RfidSimulatorUI/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            path = urlsplit(self.path).path
            if path == "/api/state":
                self._json(HTTPStatus.OK, {"ok": True, "data": controller.state()})
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

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
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
