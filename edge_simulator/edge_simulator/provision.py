from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .clients import ApiError, BackendHttpClient
from .config import ConfigError, SimulatorConfig


@dataclass(frozen=True)
class ProvisionResult:
    scenic_area_id: int
    spot_id: int
    spot_ids: dict[str, int]
    wristband_id: int
    user_id: int | None
    tag_ids: dict[str, int]
    actions: tuple[str, ...]


class AdminProvisioner:
    """Idempotently creates the database records required by the simulator."""

    TAG_FIELDS = {
        "uhf_a": "uhf_a_uid",
        "uhf_b": "uhf_b_uid",
        "uhf_c": "uhf_c_uid",
        "hf": "hf_uid",
    }

    def __init__(self, config: SimulatorConfig, username: str, password: str, log=None):
        self.config = config
        self.username = username
        self.password = password
        self.log = log or (lambda *_: None)
        self.http = BackendHttpClient(config.backend, self.log)
        self.token: str | None = None
        self.actions: list[str] = []

    def login(self) -> None:
        result = self.http.request_json(
            "POST", "/api/admin/auth/login", {"username": self.username, "password": self.password}
        )
        if not isinstance(result, dict) or not result.get("token"):
            raise ApiError("管理员登录接口未返回 token", payload=result)
        self.token = str(result["token"])
        self.log("INFO", "PROVISION", f"管理员 {self.username} 登录成功", None)

    def provision(self) -> ProvisionResult:
        if not self.token:
            self.login()
        provision = self.config.provision
        area_input = provision.get("scenic_area", {})
        spot_input = provision.get("spot", {})
        if not isinstance(area_input, dict) or not isinstance(spot_input, dict):
            raise ConfigError("provision.scenic_area 与 provision.spot 必须是对象")
        area_id_text = str(area_input.get("area_id", "SIM-AREA-001")).strip()
        area = self._ensure_row(
            "scenic_areas",
            "area_id",
            area_id_text,
            {
                "area_id": area_id_text,
                "name": area_input.get("name", "模拟测试景区"),
                "region": area_input.get("region", "测试环境"),
                "description": area_input.get("description", "由边缘设备模拟器创建"),
                "status": area_input.get("status", "active"),
            },
        )
        area_db_id = self._row_id(area, "scenic_areas")
        spot_ids: dict[str, int] = {}
        if self.config.attractions:
            for attraction in self.config.attractions:
                spot = self._ensure_row(
                    "spots",
                    "spot_id",
                    attraction.spot_id,
                    {
                        "scenic_area_id": area_db_id,
                        "spot_id": attraction.spot_id,
                        "site": attraction.name,
                        "description": f"{attraction.district}｜{attraction.description}",
                        "latitude": attraction.latitude,
                        "longitude": attraction.longitude,
                        "status": "active",
                    },
                )
                spot_ids[attraction.id] = self._row_id(spot, "spots")
        else:
            spot_id_text = str(spot_input.get("spot_id", "SIM-SPOT-001")).strip()
            spot = self._ensure_row(
                "spots",
                "spot_id",
                spot_id_text,
                {
                    "scenic_area_id": area_db_id,
                    "spot_id": spot_id_text,
                    "site": spot_input.get("site", "模拟互动点"),
                    "description": spot_input.get("description", "由边缘设备模拟器创建"),
                    "latitude": spot_input.get("latitude", 30.6586),
                    "longitude": spot_input.get("longitude", 104.0647),
                    "status": spot_input.get("status", "active"),
                },
            )
            spot_ids["default"] = self._row_id(spot, "spots")
        spot_db_id = next(iter(spot_ids.values()))

        for reader in self.config.readers:
            self._ensure_row(
                "readers",
                "device_id",
                reader.device_id,
                {
                    "device_id": reader.device_id,
                    "spot_id": spot_ids.get(reader.attraction_id or "default", spot_db_id),
                    "device_type": reader.device_type,
                    "name": reader.name,
                    "status": "offline",
                },
            )
        for device in self.config.devices:
            self._ensure_row(
                "devices",
                "device_id",
                device.device_id,
                {
                    "device_id": device.device_id,
                    "spot_id": spot_ids.get(device.attraction_id or "default", spot_db_id),
                    "device_type": device.device_type.lower(),
                    "name": device.name,
                    "status": "offline",
                    "config_json": json.dumps(device.config, ensure_ascii=False, separators=(",", ":")),
                },
            )

        payload = {
            "version": int(self.config.wristband.get("version", 1)),
            "type": self.config.wristband.get("type", "wristband"),
            "uid": self.config.wristband.get("uid") or self.config.wristband["qr_code"],
            "qr_code": self.config.wristband["qr_code"],
            "uhf_a_uid": self.config.wristband["uhf_a_uid"],
            "uhf_b_uid": self.config.wristband["uhf_b_uid"],
            "uhf_c_uid": self.config.wristband["uhf_c_uid"],
            "hf_uid": self.config.wristband["hf_uid"],
        }
        imported = self._api("POST", "/api/admin/wristbands/import", payload)
        if not isinstance(imported, dict):
            raise ApiError("手环导入接口返回格式异常", payload=imported)
        wristband_id = int(imported.get("wristband_id") or imported.get("wristbandId"))
        already = bool(imported.get("already_imported") or imported.get("alreadyImported"))
        self.actions.append(f"手环 {wristband_id} {'已存在' if already else '已导入'}")

        user_id_raw = provision.get("user_id", self.config.app.user_id)
        user_id = int(user_id_raw) if user_id_raw not in (None, "") else None
        if user_id is not None:
            self._api("GET", f"/api/admin/tables/users/rows/{user_id}")
            self._api(
                "PUT",
                f"/api/admin/tables/wristbands/rows/{wristband_id}",
                {"owner_id": user_id, "status": "active"},
            )
            self.actions.append(f"手环 {wristband_id} 已绑定用户 {user_id}")

        tag_ids: dict[str, int] = {}
        for tag_name, field in self.TAG_FIELDS.items():
            tag = self._find_exact("tags", "tag_uid", str(self.config.wristband[field]))
            if tag is None:
                raise RuntimeError(f"导入后未找到标签：{field}")
            tag_ids[tag_name] = self._row_id(tag, "tags")

        for binding in self.config.bindings:
            tag_id = tag_ids[binding["tag"]]
            row = self._find_exact("interaction_bindings", "tag_id", tag_id)
            values = {
                "tag_id": tag_id,
                "device_type": binding["device_type"],
                "action": binding["action"],
                "params_template": json.dumps(
                    binding.get("params_template") or {}, ensure_ascii=False, separators=(",", ":")
                ),
                "is_active": 1 if binding.get("is_active", True) else 0,
            }
            if row is None:
                created = self._api("POST", "/api/admin/tables/interaction_bindings/rows", values)
                self.actions.append(f"已创建 {binding['tag']} 交互绑定 #{self._row_id(created, 'interaction_bindings')}")
            else:
                binding_id = self._row_id(row, "interaction_bindings")
                self._api("PUT", f"/api/admin/tables/interaction_bindings/rows/{binding_id}", values)
                self.actions.append(f"已更新 {binding['tag']} 交互绑定 #{binding_id}")

        return ProvisionResult(
            scenic_area_id=area_db_id,
            spot_id=spot_db_id,
            spot_ids=spot_ids,
            wristband_id=wristband_id,
            user_id=user_id,
            tag_ids=tag_ids,
            actions=tuple(self.actions),
        )

    def audit(self) -> dict[str, Any]:
        if not self.token:
            self.login()
        missing: list[str] = []
        found: dict[str, Any] = {}
        for node in self.config.all_nodes:
            table = "readers" if node in self.config.readers else "devices"
            row = self._find_exact(table, "device_id", node.device_id)
            if row is None:
                missing.append(f"{table}:{node.device_id}")
            else:
                found[node.device_id] = row
        wristband = self._find_exact("wristbands", "qr_code", self.config.wristband["qr_code"])
        if wristband is None:
            missing.append(f"wristband:{self.config.wristband['qr_code']}")
        else:
            found["wristband"] = wristband
        return {"ok": not missing, "missing": missing, "found": found}

    def _ensure_row(
        self, table: str, identity_field: str, identity_value: Any, values: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self._find_exact(table, identity_field, identity_value)
        if existing is None:
            row = self._api("POST", f"/api/admin/tables/{table}/rows", values)
            self.actions.append(f"已创建 {table}:{identity_value}")
            return row
        row_id = self._row_id(existing, table)
        row = self._api("PUT", f"/api/admin/tables/{table}/rows/{row_id}", values)
        self.actions.append(f"已复用并同步 {table}:{identity_value}")
        return row

    def _find_exact(self, table: str, field: str, value: Any) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({"page": 1, "size": 100, "keyword": str(value)})
        result = self._api("GET", f"/api/admin/tables/{table}/rows?{query}")
        rows = result.get("rows", []) if isinstance(result, dict) else []
        for row in rows:
            if str(row.get(field)) == str(value):
                return row
        return None

    def _api(self, method: str, path: str, payload: Any = None) -> Any:
        if not self.token:
            raise RuntimeError("尚未登录管理员")
        return self.http.request_json(method, path, payload, token=self.token)

    @staticmethod
    def _row_id(row: Any, table: str) -> int:
        if not isinstance(row, dict) or row.get("id") is None:
            raise ApiError(f"{table} 接口返回中缺少 id", payload=row)
        return int(row["id"])
