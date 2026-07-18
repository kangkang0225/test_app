from __future__ import annotations

import json
import socket
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge_simulator.clients import TcpNode
from edge_simulator.config import AppSettings, BackendSettings, NodeSettings, SimulatorConfig
from edge_simulator.protocol import event_batch_body
from edge_simulator.protocol import SIMULATED_JPEG
from edge_simulator.runtime import SimulatorRuntime


class FakeEdgeServer:
    def __init__(self, *, command_after_heartbeat: dict | None = None):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.received: list[dict] = []
        self.command_after_heartbeat = command_after_heartbeat
        self.ack_received = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(1)

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass
        self.thread.join(1)

    def _run(self) -> None:
        self.ready.set()
        try:
            connection, _ = self.server.accept()
        except OSError:
            return
        with connection:
            buffer = b""
            while True:
                try:
                    chunk = connection.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    message = json.loads(raw)
                    self.received.append(message)
                    path = message["path"]
                    response_type = {
                        "/api/edge/heartbeat": "heartbeat_ack",
                        "/api/edge/event-batches": "batch_ack",
                        "/api/edge/command-ack": "command_ack",
                    }[path]
                    response = {"type": response_type, "body": {"code": 200, "data": {"accepted": True}}}
                    connection.sendall((json.dumps(response) + "\n").encode())
                    if path == "/api/edge/heartbeat" and self.command_after_heartbeat:
                        command = {"type": "command", "body": self.command_after_heartbeat}
                        connection.sendall((json.dumps(command) + "\n").encode())
                        self.command_after_heartbeat = None
                    if path == "/api/edge/command-ack":
                        self.ack_received.set()


class TcpClientTests(unittest.TestCase):
    def test_heartbeat_and_batch_roundtrip(self) -> None:
        server = FakeEdgeServer()
        server.start()
        backend = BackendSettings(
            http_base="http://127.0.0.1:1",
            tcp_host="127.0.0.1",
            tcp_port=server.port,
            connect_timeout_seconds=1,
            response_timeout_seconds=1,
        )
        node = TcpNode(backend, NodeSettings("reader", "READER-1", "UHF"))
        try:
            heartbeat = node.connect()
            self.assertEqual(heartbeat["type"], "heartbeat_ack")
            batch = event_batch_body("READER-1", [("TAG-1", datetime.now().astimezone(), -40)])
            response = node.request("/api/edge/event-batches", batch, {"batch_ack"})
            self.assertEqual(response["type"], "batch_ack")
            self.assertEqual(server.received[0]["path"], "/api/edge/heartbeat")
            self.assertEqual(server.received[1]["body"]["reader_id"], "READER-1")
        finally:
            node.close()
            server.close()

    def test_runtime_auto_acknowledges_downstream_command(self) -> None:
        server = FakeEdgeServer(
            command_after_heartbeat={
                "command_type": "UHF",
                "command_id": 88,
                "device_id": "LIGHT-1",
                "action": "on",
                "params": {"color": "warm"},
            }
        )
        server.start()
        backend = BackendSettings(
            http_base="http://127.0.0.1:1",
            tcp_host="127.0.0.1",
            tcp_port=server.port,
            connect_timeout_seconds=1,
            response_timeout_seconds=1,
            heartbeat_interval_seconds=30,
        )
        device = NodeSettings("light", "LIGHT-1", "LIGHT", auto_ack="success", ack_delay_ms=0)
        config = SimulatorConfig(
            path=Path("fake.json"),
            backend=backend,
            wristband={},
            readers=(),
            devices=(device,),
            app=AppSettings(),
        )
        runtime = SimulatorRuntime(config)
        try:
            runtime.devices["light"].connect()
            self.assertTrue(server.ack_received.wait(2))
            ack = next(item for item in server.received if item["path"] == "/api/edge/command-ack")
            self.assertEqual(ack["body"]["command_id"], 88)
            self.assertEqual(ack["body"]["status"], "success")
        finally:
            runtime.close()
            server.close()

    def test_camera_command_ack_then_uploads_multipart_image(self) -> None:
        upload_received = threading.Event()
        upload: dict[str, object] = {}

        class UploadHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers["Content-Length"])
                upload["path"] = self.path
                upload["content_type"] = self.headers["Content-Type"]
                upload["body"] = self.rfile.read(length)
                response = json.dumps({"code": 200, "message": "ok", "data": {"image_id": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                upload_received.set()

            def log_message(self, *_args) -> None:
                return

        image_server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
        image_thread = threading.Thread(target=image_server.serve_forever, daemon=True)
        image_thread.start()
        edge_server = FakeEdgeServer(
            command_after_heartbeat={
                "command_type": "UHF",
                "command_id": 99,
                "device_id": "CAMERA-1",
                "action": "capture",
                "params": {},
            }
        )
        edge_server.start()
        backend = BackendSettings(
            http_base=f"http://127.0.0.1:{image_server.server_port}",
            tcp_host="127.0.0.1",
            tcp_port=edge_server.port,
            connect_timeout_seconds=1,
            response_timeout_seconds=1,
            heartbeat_interval_seconds=30,
        )
        camera = NodeSettings(
            "camera", "CAMERA-1", "CAMERA", auto_ack="success", ack_delay_ms=0, auto_upload=True
        )
        config = SimulatorConfig(
            path=Path("fake.json"),
            backend=backend,
            wristband={},
            readers=(),
            devices=(camera,),
            app=AppSettings(),
        )
        runtime = SimulatorRuntime(config)
        try:
            runtime.devices["camera"].connect()
            self.assertTrue(upload_received.wait(2))
            self.assertEqual(upload["path"], "/api/edge/images/upload")
            self.assertIn("multipart/form-data", str(upload["content_type"]))
            self.assertIn(b'name="deviceId"', upload["body"])
            self.assertIn(b"CAMERA-1", upload["body"])
            self.assertIn(b"99", upload["body"])
            self.assertIn(SIMULATED_JPEG, upload["body"])
        finally:
            runtime.close()
            edge_server.close()
            image_server.shutdown()
            image_server.server_close()
            image_thread.join(1)


if __name__ == "__main__":
    unittest.main()
