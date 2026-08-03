from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from edge_simulator.config import load_config
from edge_simulator.provision import AdminProvisioner


class FakeAdminApi:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {
            "users": [
                {"id": 1, "openid": "sim-user"},
                {"id": 2, "openid": "dufu-app-user"},
            ],
            "scenic_areas": [],
            "spots": [],
            "readers": [],
            "devices": [],
            "wristbands": [],
            "tags": [],
            "interaction_bindings": [],
        }
        self.next_id = {table: 1 for table in self.tables}

    def request_json(self, method, path, payload=None, *, token=None, **_kwargs):
        if path == "/api/admin/auth/login":
            return {"token": "admin-token", "profile": {"id": 1}}
        if path == "/api/admin/wristbands/import":
            return self._import_wristband(payload)
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if parts[:3] != ["api", "admin", "tables"] or len(parts) < 5:
            raise AssertionError(f"Unexpected request: {method} {path}")
        table = parts[3]
        rows = self.tables[table]
        if method == "GET" and len(parts) == 5:
            return {"rows": [dict(row) for row in rows], "total": len(rows), "page": 1, "size": 100}
        if method == "GET" and len(parts) == 6:
            return dict(self._by_id(table, int(parts[5])))
        if method == "POST" and len(parts) == 5:
            row = {"id": self._new_id(table), **payload}
            rows.append(row)
            return dict(row)
        if method == "PUT" and len(parts) == 6:
            row = self._by_id(table, int(parts[5]))
            row.update(payload)
            return dict(row)
        raise AssertionError(f"Unexpected request: {method} {path}")

    def _import_wristband(self, payload):
        for row in self.tables["wristbands"]:
            if row["qr_code"] == payload["qr_code"]:
                return {"wristband_id": row["id"], "already_imported": True, "tag_count": 4}
        wristband = {"id": self._new_id("wristbands"), **payload, "owner_id": None, "status": "active"}
        self.tables["wristbands"].append(wristband)
        for tag_type, field in (
            ("UHF-A", "uhf_a_uid"),
            ("UHF-B", "uhf_b_uid"),
            ("UHF-C", "uhf_c_uid"),
            ("HF", "hf_uid"),
        ):
            self.tables["tags"].append(
                {
                    "id": self._new_id("tags"),
                    "wristband_id": wristband["id"],
                    "tag_uid": payload[field],
                    "tag_type": tag_type,
                    "status": "active",
                }
            )
        return {"wristband_id": wristband["id"], "already_imported": False, "tag_count": 4}

    def _new_id(self, table: str) -> int:
        value = self.next_id[table]
        self.next_id[table] += 1
        return value

    def _by_id(self, table: str, row_id: int) -> dict:
        return next(row for row in self.tables[table] if row["id"] == row_id)


class ProvisionTests(unittest.TestCase):
    def test_provision_is_idempotent_and_builds_complete_dataset(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.example.json")
        fake = FakeAdminApi()

        first = AdminProvisioner(config, "admin", "secret")
        first.http = fake
        first_result = first.provision()

        # 模拟用户已经在 App 中切换 UHF-B，第二次 provision 必须保留它。
        uhf_b_tag_id = first_result.tag_ids["uhf_b"]
        next(
            row for row in fake.tables["interaction_bindings"]
            if row["tag_id"] == uhf_b_tag_id
        )["device_type"] = "light"

        second = AdminProvisioner(config, "admin", "secret")
        second.http = fake
        second_result = second.provision()

        self.assertEqual(first_result.wristband_id, second_result.wristband_id)
        self.assertEqual(first_result.user_id, 1)
        self.assertEqual(len(fake.tables["scenic_areas"]), 1)
        self.assertEqual(len(fake.tables["spots"]), 1)
        self.assertEqual(len(fake.tables["readers"]), 2)
        self.assertEqual(
            next(row for row in fake.tables["readers"] if row["device_type"] == "HF")["hf_purpose"],
            "control",
        )
        self.assertEqual(len(fake.tables["devices"]), 2)
        self.assertEqual(len(fake.tables["wristbands"]), 1)
        self.assertEqual(len(fake.tables["tags"]), 4)
        self.assertEqual(len(fake.tables["interaction_bindings"]), 2)
        self.assertEqual(
            next(
                row for row in fake.tables["interaction_bindings"]
                if row["tag_id"] == uhf_b_tag_id
            )["device_type"],
            "light",
        )
        self.assertEqual(fake.tables["wristbands"][0]["owner_id"], 1)
        self.assertEqual({row["device_type"] for row in fake.tables["devices"]}, {"camera", "light"})
        single_point_hf_devices = [
            row for row in fake.tables["devices"]
            if json.loads(row["config_json"])["hf_control"]
        ]
        self.assertEqual([row["device_id"] for row in single_point_hf_devices], ["SIM-LIGHT-001"])
        self.assertEqual(set(first_result.tag_ids), {"uhf_a", "uhf_b", "uhf_c", "hf"})

    def test_multi_attraction_provision_assigns_nodes_to_eight_spots(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.web.example.json")
        fake = FakeAdminApi()
        provisioner = AdminProvisioner(config, "admin", "secret")
        provisioner.http = fake
        result = provisioner.provision()

        self.assertEqual(len(result.spot_ids), 8)
        self.assertEqual(len(fake.tables["spots"]), 8)
        self.assertEqual(len(fake.tables["readers"]), 21)
        hf_purposes = {
            row["device_id"]: row["hf_purpose"]
            for row in fake.tables["readers"]
            if row["device_type"] == "HF"
        }
        self.assertEqual(hf_purposes["SIM-DUFU-GONGBUCI-HF"], "checkin")
        self.assertEqual(hf_purposes["SIM-DUFU-CHAIMEN-HF-CHECKIN"], "checkin")
        self.assertEqual(hf_purposes["SIM-DUFU-SHISHITANG-HF-CONTROL"], "control")
        self.assertEqual(hf_purposes["SIM-DUFU-MAOWU-HF"], "control")
        self.assertEqual(len(fake.tables["devices"]), 10)
        shishitang_spot = result.spot_ids["shishitang"]
        shishitang_nodes = [
            row for row in fake.tables["readers"] + fake.tables["devices"]
            if row["spot_id"] == shishitang_spot
        ]
        self.assertEqual({row["device_id"] for row in shishitang_nodes}, {
            "SIM-DUFU-SHISHITANG-UHF",
            "SIM-DUFU-SHISHITANG-HF",
            "SIM-DUFU-SHISHITANG-HF-CONTROL",
            "SIM-DUFU-SHISHITANG-CAMERA",
        })
        shishitang_camera = next(
            row for row in fake.tables["devices"]
            if row["device_id"] == "SIM-DUFU-SHISHITANG-CAMERA"
        )
        maowu_spray = next(
            row for row in fake.tables["devices"]
            if row["device_id"] == "SIM-DUFU-MAOWU-SPRAY"
        )
        wanfolou_speaker = next(
            row for row in fake.tables["devices"]
            if row["device_id"] == "SIM-DUFU-WANFOLOU-SPEAKER"
        )
        wanfolou_devices = [
            row for row in fake.tables["devices"]
            if row["spot_id"] == result.spot_ids["wanfolou"]
        ]
        self.assertEqual(json.loads(shishitang_camera["config_json"])["interaction_tags"], ["UHF-B", "UHF-C"])
        self.assertTrue(json.loads(shishitang_camera["config_json"])["hf_control"])
        self.assertTrue(json.loads(shishitang_camera["config_json"])["uhf_requires_hf_authorization"])
        self.assertEqual(json.loads(maowu_spray["config_json"])["interaction_tags"], ["UHF-B", "UHF-C"])
        self.assertTrue(json.loads(maowu_spray["config_json"])["hf_control"])
        self.assertTrue(json.loads(wanfolou_speaker["config_json"])["hf_control"])
        self.assertEqual(
            {row["device_type"] for row in wanfolou_devices},
            {"camera", "light", "speaker"},
        )
        self.assertTrue(all(
            json.loads(row["config_json"])["interaction_tags"] == ["UHF-B", "UHF-C"]
            for row in wanfolou_devices
        ))


if __name__ == "__main__":
    unittest.main()
