from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edge_simulator.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.example.json")
        self.assertEqual(config.backend.tcp_port, 9002)
        self.assertEqual(config.reader_for_type("uhf").device_id, "SIM-UHF-READER-001")
        self.assertEqual(config.reader_for_type("HF").device_id, "SIM-HF-READER-001")
        self.assertEqual(config.wristband["uhf_b_uid"], "E200SIM000000000000000B1")
        self.assertTrue(next(device for device in config.devices if device.name == "camera").auto_upload)

    def test_chengdu_web_config_has_eight_valid_attractions(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.web.example.json")
        self.assertEqual(len(config.attractions), 8)
        self.assertEqual(config.attraction("wuhouci").tags, ("uhf_a",))
        self.assertEqual(
            config.attraction("qingcheng").tags, ("uhf_a", "uhf_b", "uhf_c", "hf")
        )
        self.assertEqual(
            config.reader_for_type("HF", "kuanzhai").device_id, "SIM-CD-KUANZHAI-HF"
        )
        self.assertEqual(len(config.nodes_for_attraction("jinsha")), 3)

    def test_wristband_payload_file_and_camel_case_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "version": 1,
                "type": "wristband",
                "uid": "WB-1",
                "qrCode": "QR-1",
                "uhfAUid": "A",
                "uhfBUid": "B",
                "uhfCUid": "C",
                "hfUid": "H",
            }
            (root / "wristband.json").write_text(json.dumps(payload), encoding="utf-8")
            config = {
                "backend": {"http_base": "http://localhost:1", "tcp_host": "localhost", "tcp_port": 2},
                "wristband": {"payload_file": "wristband.json"},
                "readers": [
                    {"name": "u", "device_id": "U", "device_type": "UHF"},
                    {"name": "h", "device_id": "H", "device_type": "HF"},
                ],
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_config(config_path)
            self.assertEqual(loaded.wristband["qr_code"], "QR-1")
            self.assertEqual(loaded.wristband["hf_uid"], "H")

    def test_duplicate_node_device_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "backend": {"http_base": "http://localhost", "tcp_host": "x", "tcp_port": 1},
                        "wristband": {
                            "qr_code": "Q",
                            "uhf_a_uid": "A",
                            "uhf_b_uid": "B",
                            "uhf_c_uid": "C",
                            "hf_uid": "H",
                        },
                        "readers": [
                            {"name": "u", "device_id": "DUP", "device_type": "UHF"},
                            {"name": "h", "device_id": "HF", "device_type": "HF"},
                        ],
                        "devices": [{"name": "d", "device_id": "DUP", "device_type": "light"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "device_id"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
