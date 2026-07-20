from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from pathlib import Path

from edge_simulator.config import load_config
from edge_simulator.web import AttractionController, create_server


class SilentLogs:
    def __init__(self):
        self.items = []

    def add(self, level, source, message, data=None):
        self.items.append((level, source, message, data))

    def recent(self, _limit):
        return []


class FakeRuntime:
    def __init__(self, config, *, online=True):
        self.config = config
        self.online = online
        self.logs = SilentLogs()
        self.sent = []
        self.closed = False

    def connect_all(self):
        self.online = True

    def disconnect_all(self):
        self.online = False

    def close(self):
        self.closed = True

    def send_tag(self, tag_name, **kwargs):
        self.sent.append((tag_name, kwargs))
        return {"type": "batch_ack", "body": {"accepted": 1}}

    def simulate_dwell(self, seconds, **kwargs):
        self.sent.append(("dwell", {"seconds": seconds, **kwargs}))
        return {"type": "batch_ack", "body": {"accepted": 2}}

    def send_control_command(self, device_id, action, params):
        self.sent.append(("control", {"device_id": device_id, "action": action, "params": params}))
        return {"command_id": 99, "command_status": "sent"}

    def interaction_bindings(self):
        return [
            {
                "id": 1,
                "tagType": "UHF-B",
                "tagLabel": "抬腕",
                "configured": True,
                "deviceType": "camera",
                "action": "capture",
                "active": True,
            },
            {
                "id": 2,
                "tagType": "UHF-C",
                "tagLabel": "按键",
                "configured": True,
                "deviceType": "light",
                "action": "on",
                "active": True,
            },
        ]

    def capture_records(self):
        return {}

    def status_rows(self):
        return [
            {
                "name": node.name,
                "device_id": node.device_id,
                "role": "reader" if node in self.config.readers else "device",
                "type": node.device_type,
                "attraction": node.attraction_id or "-",
                "status": "online" if self.online else "offline",
                "ack": "success" if node in self.config.devices else "-",
            }
            for node in self.config.all_nodes
        ]


class WebControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(Path(__file__).parents[1] / "config.web.example.json")

    def test_clicking_attraction_enters_then_leaves(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)

        entered = controller.toggle("shishitang")
        self.assertEqual(entered["action"], "enter")
        self.assertEqual([item[0] for item in runtime.sent], ["dwell"])
        self.assertEqual(runtime.sent[0][1]["attraction_id"], "shishitang")
        self.assertEqual(runtime.sent[0][1]["seconds"], 38)
        self.assertEqual(entered["state"]["active_attraction_id"], "shishitang")

        triggered = controller.trigger("shishitang", "uhf_b")
        self.assertIn("UHF-B", triggered["message"])
        self.assertEqual(runtime.sent[-1][0], "uhf_b")

        left = controller.toggle("shishitang")
        self.assertEqual(left["action"], "leave")
        self.assertEqual(runtime.sent[-1][0], "uhf_a")
        self.assertEqual(runtime.sent[-1][1]["event_type"], "leave")
        self.assertIsNone(left["state"]["active_attraction_id"])

    def test_entering_another_attraction_auto_leaves_previous(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)
        controller.toggle("daxie")
        result = controller.toggle("chaimen")

        self.assertEqual(result["action"], "enter")
        self.assertIn("已自动离开 大廨", result["warnings"])
        self.assertEqual([item[0] for item in runtime.sent], ["dwell", "uhf_a", "dwell"])
        self.assertEqual(runtime.sent[1][1]["event_type"], "leave")
        self.assertEqual(result["state"]["active_attraction_id"], "chaimen")

    def test_hf_authorization_unlocks_configured_environment_controls(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)
        controller.toggle("maowu")

        with self.assertRaisesRegex(RuntimeError, "HF"):
            controller.control("maowu", "SIM-DUFU-MAOWU-SPRAY", "spray-on")

        controller.trigger("maowu", "hf_control")
        result = controller.control("maowu", "SIM-DUFU-MAOWU-SPRAY", "spray-on")
        self.assertIn("开启喷雾", result["message"])
        self.assertEqual(runtime.sent[-1], (
            "control",
            {"device_id": "SIM-DUFU-MAOWU-SPRAY", "action": "on", "params": {}},
        ))

    def test_fixed_inventory_reports_missing_dynamic_binding_without_creating_device(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)
        controller.toggle("daxie")

        before = len(self.config.devices)
        result = controller.trigger("daxie", "uhf_b")

        self.assertIn("未安装", result["message"])
        self.assertEqual(len(self.config.devices), before)
        daxie = next(item for item in result["state"]["attractions"] if item["id"] == "daxie")
        uhf_b = next(item for item in daxie["interaction_bindings"] if item["tag"] == "uhf_b")
        self.assertEqual(uhf_b["device_type"], "camera")
        self.assertFalse(uhf_b["installed"])

    def test_hf_checkin_progress_is_distinct_and_uses_eighty_percent_threshold(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)
        controller.toggle("daxie")
        first = controller.trigger("daxie", "hf_checkin")
        repeated = controller.trigger("daxie", "hf_checkin")

        self.assertEqual(first["state"]["journey"]["completed_spots"], 1)
        self.assertEqual(repeated["state"]["journey"]["completed_spots"], 1)
        self.assertEqual(repeated["state"]["journey"]["required_spots"], 7)
        self.assertEqual(runtime.sent[-1][1]["hf_purpose"], "checkin")

    def test_http_server_serves_interface_and_state_api(self) -> None:
        runtime = FakeRuntime(self.config, online=False)
        server, controller = create_server(self.config, "127.0.0.1", 0, runtime=runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as response:
                html = response.read().decode("utf-8")
                self.assertIn(
                    "img-src 'self' data:",
                    response.headers.get("Content-Security-Policy", ""),
                )
            self.assertIn("杜甫草堂游踪实验台", html)

            with urllib.request.urlopen(base + "/api/state", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["data"]["attractions"]), 8)

            with urllib.request.urlopen(base + "/media/captures/SIM-DUFU-SHAOLING-CAMERA", timeout=2) as response:
                image = response.read()
            self.assertGreater(len(image), 100_000)

            request = urllib.request.Request(base + "/api/connect", data=b"{}", method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                connected = json.loads(response.read())
            self.assertTrue(connected["data"]["connected"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
            controller.close()


    def test_http_server_rejects_a_second_listener_on_the_same_port(self) -> None:
        first_runtime = FakeRuntime(self.config, online=False)
        first_server, first_controller = create_server(
            self.config, "127.0.0.1", 0, runtime=first_runtime
        )
        try:
            second_runtime = FakeRuntime(self.config, online=False)
            with self.assertRaises(OSError):
                create_server(
                    self.config,
                    "127.0.0.1",
                    first_server.server_port,
                    runtime=second_runtime,
                )
        finally:
            first_server.server_close()
            first_controller.close()


if __name__ == "__main__":
    unittest.main()
