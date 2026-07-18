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

        entered = controller.toggle("panda")
        self.assertEqual(entered["action"], "enter")
        self.assertEqual([item[0] for item in runtime.sent], ["dwell", "uhf_b"])
        self.assertEqual(runtime.sent[0][1]["attraction_id"], "panda")
        self.assertEqual(runtime.sent[0][1]["seconds"], 42)
        self.assertEqual(entered["state"]["active_attraction_id"], "panda")

        left = controller.toggle("panda")
        self.assertEqual(left["action"], "leave")
        self.assertEqual(runtime.sent[-1][0], "uhf_a")
        self.assertIsNone(left["state"]["active_attraction_id"])

    def test_entering_another_attraction_auto_leaves_previous(self) -> None:
        runtime = FakeRuntime(self.config)
        controller = AttractionController(self.config, runtime)
        controller.toggle("wuhouci")
        result = controller.toggle("dufu")

        self.assertEqual(result["action"], "enter")
        self.assertIn("已自动离开 武侯祠", result["warnings"])
        self.assertEqual([item[0] for item in runtime.sent], ["dwell", "uhf_a", "dwell", "uhf_c"])
        self.assertEqual(result["state"]["active_attraction_id"], "dufu")

    def test_http_server_serves_interface_and_state_api(self) -> None:
        runtime = FakeRuntime(self.config, online=False)
        server, controller = create_server(self.config, "127.0.0.1", 0, runtime=runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as response:
                html = response.read().decode("utf-8")
            self.assertIn("成都腕带漫游实验台", html)

            with urllib.request.urlopen(base + "/api/state", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["data"]["attractions"]), 8)

            request = urllib.request.Request(base + "/api/connect", data=b"{}", method="POST")
            with urllib.request.urlopen(request, timeout=2) as response:
                connected = json.loads(response.read())
            self.assertTrue(connected["data"]["connected"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
            controller.close()


if __name__ == "__main__":
    unittest.main()
