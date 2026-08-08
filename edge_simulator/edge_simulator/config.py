from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when simulator configuration is incomplete or inconsistent."""


@dataclass(frozen=True)
class BackendSettings:
    http_base: str
    tcp_host: str
    tcp_port: int
    connect_timeout_seconds: float = 5.0
    response_timeout_seconds: float = 5.0
    heartbeat_interval_seconds: float = 15.0


@dataclass(frozen=True)
class NodeSettings:
    name: str
    device_id: str
    device_type: str
    hf_purpose: str | None = None
    auto_ack: str = "success"
    ack_delay_ms: int = 100
    auto_upload: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    attraction_id: str | None = None
    image_path: Path | None = None


@dataclass(frozen=True)
class AttractionSettings:
    id: str
    name: str
    district: str
    description: str
    spot_id: str
    tags: tuple[str, ...]
    simulated_dwell_seconds: float = 35.0
    accent: str = "#C85B3C"
    latitude: float | None = None
    longitude: float | None = None
    image_path: Path | None = None


@dataclass(frozen=True)
class UiSettings:
    eyebrow: str = "RFID SCENIC LAB"
    title: str = "景区腕带游踪模拟器"
    description: str = "进入景点时自动上报 UHF-A，其余标签与环境设备由测试员手动触发。"
    guide_title: str = "景区导览图"
    guide_image_path: Path | None = None


@dataclass(frozen=True)
class AppSettings:
    token_env: str = "SIM_APP_TOKEN"
    jwt_secret_env: str = "SIM_JWT_SECRET"
    user_id: int | None = None
    jwt_algorithm: str = "auto"
    token_ttl_seconds: int = 3600


@dataclass(frozen=True)
class SimulatorConfig:
    path: Path
    backend: BackendSettings
    wristband: dict[str, Any]
    readers: tuple[NodeSettings, ...]
    devices: tuple[NodeSettings, ...]
    attractions: tuple[AttractionSettings, ...] = ()
    bindings: tuple[dict[str, Any], ...] = ()
    ui: UiSettings = field(default_factory=UiSettings)
    app: AppSettings = field(default_factory=AppSettings)
    provision: dict[str, Any] = field(default_factory=dict)
    scenario: dict[str, Any] = field(default_factory=dict)

    @property
    def all_nodes(self) -> tuple[NodeSettings, ...]:
        return self.readers + self.devices

    def reader_for_type(
        self,
        device_type: str,
        attraction_id: str | None = None,
        *,
        hf_purpose: str | None = None,
    ) -> NodeSettings:
        wanted = device_type.upper()
        purpose = hf_purpose.lower() if hf_purpose else None
        for reader in self.readers:
            if reader.device_type == wanted and (
                attraction_id is None or reader.attraction_id == attraction_id
            ) and (purpose is None or reader.hf_purpose == purpose):
                return reader
        suffix = f"（景点 {attraction_id}）" if attraction_id else ""
        purpose_suffix = f"，用途 {purpose}" if purpose else ""
        raise ConfigError(f"未配置 {wanted} 类型的读写器{suffix}{purpose_suffix}")

    def attraction(self, attraction_id: str) -> AttractionSettings:
        for attraction in self.attractions:
            if attraction.id == attraction_id:
                return attraction
        raise ConfigError(f"未配置景点：{attraction_id}")

    def nodes_for_attraction(self, attraction_id: str) -> tuple[NodeSettings, ...]:
        return tuple(node for node in self.all_nodes if node.attraction_id == attraction_id)


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置项 {field_name} 必须是非空字符串")
    return value.strip()


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"配置项 {field_name} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {field_name} 必须是正整数") from exc
    if parsed <= 0:
        raise ConfigError(f"配置项 {field_name} 必须是正整数")
    return parsed


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")
    return raw


def _load_wristband(raw: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    wristband = raw.get("wristband")
    if not isinstance(wristband, dict):
        raise ConfigError("配置项 wristband 必须是对象")
    result = dict(wristband)
    payload_file = result.pop("payload_file", None)
    if payload_file:
        payload_path = Path(str(payload_file))
        if not payload_path.is_absolute():
            payload_path = config_dir / payload_path
        payload = _load_json(payload_path.resolve())
        result = {**payload, **result}

    aliases = {
        "qrCode": "qr_code",
        "uhfAUid": "uhf_a_uid",
        "uhfBUid": "uhf_b_uid",
        "uhfCUid": "uhf_c_uid",
        "hfUid": "hf_uid",
    }
    for source, target in aliases.items():
        if source in result and target not in result:
            result[target] = result[source]

    for key in ("qr_code", "uhf_a_uid", "uhf_b_uid", "uhf_c_uid", "hf_uid"):
        result[key] = _required_string(result.get(key), f"wristband.{key}")
    result["uid"] = _required_string(result.get("uid", result["qr_code"]), "wristband.uid")
    result["version"] = int(result.get("version", 1))
    result["type"] = str(result.get("type", "wristband"))
    if result["version"] != 1 or result["type"] != "wristband":
        raise ConfigError("wristband.version 必须为 1，type 必须为 wristband")
    tag_uids = [result[key] for key in ("uhf_a_uid", "uhf_b_uid", "uhf_c_uid", "hf_uid")]
    if len(set(tag_uids)) != 4:
        raise ConfigError("wristband 的四个 RFID UID 必须互不相同")
    return result


def _resolve_media_path(
    value: Any,
    config_dir: Path,
    field_name: str,
    *,
    jpeg_only: bool = False,
) -> Path | None:
    if value in (None, ""):
        return None
    raw_path = _required_string(value, field_name)
    path = Path(raw_path)
    if not path.is_absolute():
        path = config_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"配置项 {field_name} 指向的文件不存在：{path}")
    suffix = path.suffix.lower()
    allowed = {".jpg", ".jpeg"} if jpeg_only else {".jpg", ".jpeg", ".png", ".webp"}
    if suffix not in allowed:
        raise ConfigError(f"配置项 {field_name} 仅支持：{', '.join(sorted(allowed))}")
    return path


def _load_nodes(raw: Any, section: str, config_dir: Path) -> tuple[NodeSettings, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"配置项 {section} 必须是非空数组")
    nodes: list[NodeSettings] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{section}[{index}] 必须是对象")
        prefix = f"{section}[{index}]"
        name = _required_string(item.get("name"), f"{prefix}.name").lower()
        device_id = _required_string(item.get("device_id"), f"{prefix}.device_id")
        device_type = _required_string(item.get("device_type"), f"{prefix}.device_type").upper()
        raw_hf_purpose = item.get("hf_purpose")
        hf_purpose = None
        if section == "readers" and device_type == "HF":
            hf_purpose = _required_string(
                raw_hf_purpose, f"{prefix}.hf_purpose"
            ).lower()
            if hf_purpose not in {"checkin", "control"}:
                raise ConfigError(
                    f"{prefix}.hf_purpose 只能是 checkin 或 control"
                )
        elif raw_hf_purpose is not None:
            raise ConfigError(f"{prefix} 只有 HF Reader 可以设置 hf_purpose")
        if name in seen_names:
            raise ConfigError(f"{section} 中 name 重复：{name}")
        if device_id in seen_ids:
            raise ConfigError(f"{section} 中 device_id 重复：{device_id}")
        seen_names.add(name)
        seen_ids.add(device_id)
        auto_ack = str(item.get("auto_ack", "success")).lower()
        if auto_ack not in {"success", "failed", "timeout", "rejected", "none"}:
            raise ConfigError(f"{prefix}.auto_ack 不支持：{auto_ack}")
        ack_delay_ms = int(item.get("ack_delay_ms", 100))
        if ack_delay_ms < 0:
            raise ConfigError(f"{prefix}.ack_delay_ms 不能为负数")
        node_config = item.get("config", {})
        if not isinstance(node_config, dict):
            raise ConfigError(f"{prefix}.config 必须是对象")
        if "hf_control" in node_config and not isinstance(node_config["hf_control"], bool):
            raise ConfigError(f"{prefix}.config.hf_control 必须是布尔值")
        controls = node_config.get("controls", [])
        if not isinstance(controls, list):
            raise ConfigError(f"{prefix}.config.controls 必须是数组")
        control_ids: set[str] = set()
        for control_index, control in enumerate(controls):
            control_prefix = f"{prefix}.config.controls[{control_index}]"
            if not isinstance(control, dict):
                raise ConfigError(f"{control_prefix} 必须是对象")
            control_id = _required_string(control.get("id"), f"{control_prefix}.id")
            _required_string(control.get("action"), f"{control_prefix}.action")
            if control_id in control_ids:
                raise ConfigError(f"{prefix}.config.controls 中 id 重复：{control_id}")
            control_ids.add(control_id)
            if not isinstance(control.get("params", {}), dict):
                raise ConfigError(f"{control_prefix}.params 必须是对象")
        image_path = _resolve_media_path(
            item.get("image"),
            config_dir,
            f"{prefix}.image",
            jpeg_only=True,
        )
        if image_path is not None and device_type != "CAMERA":
            raise ConfigError(f"{prefix}.image 只能配置在 camera 设备上")
        nodes.append(
            NodeSettings(
                name=name,
                device_id=device_id,
                device_type=device_type,
                hf_purpose=hf_purpose,
                auto_ack=auto_ack,
                ack_delay_ms=ack_delay_ms,
                auto_upload=bool(item.get("auto_upload", False)),
                config=dict(node_config),
                attraction_id=(
                    _required_string(item.get("attraction"), f"{prefix}.attraction").lower()
                    if item.get("attraction") is not None
                    else None
                ),
                image_path=image_path,
            )
        )
    return tuple(nodes)


def _load_attractions(raw: Any, config_dir: Path) -> tuple[AttractionSettings, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ConfigError("配置项 attractions 必须是数组")
    attractions: list[AttractionSettings] = []
    seen_ids: set[str] = set()
    allowed_tags = {"uhf_a", "uhf_b", "uhf_c", "hf"}
    tag_aliases = {"uhf-a": "uhf_a", "uhf-b": "uhf_b", "uhf-c": "uhf_c"}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"attractions[{index}] 必须是对象")
        prefix = f"attractions[{index}]"
        attraction_id = _required_string(item.get("id"), f"{prefix}.id").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", attraction_id):
            raise ConfigError(f"{prefix}.id 只能包含 2-32 位小写字母、数字、下划线或连字符")
        if attraction_id in seen_ids:
            raise ConfigError(f"attractions 中 id 重复：{attraction_id}")
        seen_ids.add(attraction_id)
        tags_raw = item.get("tags")
        if not isinstance(tags_raw, list) or not tags_raw:
            raise ConfigError(f"{prefix}.tags 必须是非空数组")
        tags: list[str] = []
        for raw_tag in tags_raw:
            tag = str(raw_tag).strip().lower()
            tag = tag_aliases.get(tag, tag)
            if tag not in allowed_tags:
                raise ConfigError(f"{prefix}.tags 包含不支持的标签：{raw_tag}")
            if tag not in tags:
                tags.append(tag)
        if "uhf_a" not in tags:
            raise ConfigError(f"{prefix}.tags 必须包含 uhf_a，用于进入/离开景点")
        dwell = float(item.get("simulated_dwell_seconds", 35))
        if dwell <= 0:
            raise ConfigError(f"{prefix}.simulated_dwell_seconds 必须大于 0")
        accent = str(item.get("accent", "#C85B3C")).upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", accent):
            raise ConfigError(f"{prefix}.accent 必须是 #RRGGBB 颜色")
        latitude = item.get("latitude")
        longitude = item.get("longitude")
        attractions.append(
            AttractionSettings(
                id=attraction_id,
                name=_required_string(item.get("name"), f"{prefix}.name"),
                district=_required_string(item.get("district", "成都"), f"{prefix}.district"),
                description=_required_string(item.get("description"), f"{prefix}.description"),
                spot_id=_required_string(
                    item.get("spot_id", f"SIM-SPOT-{attraction_id.upper()}"), f"{prefix}.spot_id"
                ),
                tags=tuple(tags),
                simulated_dwell_seconds=dwell,
                accent=accent,
                latitude=float(latitude) if latitude is not None else None,
                longitude=float(longitude) if longitude is not None else None,
                image_path=_resolve_media_path(
                    item.get("image"), config_dir, f"{prefix}.image"
                ),
            )
        )
    return tuple(attractions)


def load_config(path: str | os.PathLike[str]) -> SimulatorConfig:
    config_path = Path(path).expanduser().resolve()
    raw = _load_json(config_path)
    backend_raw = raw.get("backend")
    if not isinstance(backend_raw, dict):
        raise ConfigError("配置项 backend 必须是对象")
    http_base = _required_string(backend_raw.get("http_base"), "backend.http_base").rstrip("/")
    if not http_base.startswith(("http://", "https://")):
        raise ConfigError("backend.http_base 必须以 http:// 或 https:// 开头")
    backend = BackendSettings(
        http_base=http_base,
        tcp_host=_required_string(backend_raw.get("tcp_host"), "backend.tcp_host"),
        tcp_port=_positive_int(backend_raw.get("tcp_port"), "backend.tcp_port"),
        connect_timeout_seconds=float(backend_raw.get("connect_timeout_seconds", 5)),
        response_timeout_seconds=float(backend_raw.get("response_timeout_seconds", 5)),
        heartbeat_interval_seconds=float(backend_raw.get("heartbeat_interval_seconds", 15)),
    )
    if backend.tcp_port > 65535:
        raise ConfigError("backend.tcp_port 不能大于 65535")
    if backend.connect_timeout_seconds <= 0 or backend.response_timeout_seconds <= 0:
        raise ConfigError("backend 的连接与响应超时必须大于 0")
    if backend.heartbeat_interval_seconds <= 0:
        raise ConfigError("backend.heartbeat_interval_seconds 必须大于 0")

    wristband = _load_wristband(raw, config_path.parent)
    attractions = _load_attractions(raw.get("attractions"), config_path.parent)
    readers = _load_nodes(raw.get("readers"), "readers", config_path.parent)
    devices_raw = raw.get("devices", [])
    devices = _load_nodes(devices_raw, "devices", config_path.parent) if devices_raw else ()
    all_ids = [node.device_id for node in readers + devices]
    if len(all_ids) != len(set(all_ids)):
        raise ConfigError("readers 与 devices 中的 device_id 不能重复")
    reader_types = {reader.device_type for reader in readers}
    required_reader_types = {"UHF"}
    if not attractions or any("hf" in attraction.tags for attraction in attractions):
        required_reader_types.add("HF")
    missing_types = required_reader_types - reader_types
    if missing_types:
        raise ConfigError(f"缺少读写器类型：{', '.join(sorted(missing_types))}")

    bindings_raw = raw.get("bindings", [])
    if not isinstance(bindings_raw, list):
        raise ConfigError("配置项 bindings 必须是数组")
    allowed_tags = {"uhf_a", "uhf_b", "uhf_c", "hf"}
    bindings: list[dict[str, Any]] = []
    for index, item in enumerate(bindings_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"bindings[{index}] 必须是对象")
        binding = dict(item)
        tag = str(binding.get("tag", "")).lower()
        if tag not in allowed_tags:
            raise ConfigError(f"bindings[{index}].tag 不支持：{tag}")
        binding["tag"] = tag
        binding["device_type"] = _required_string(
            binding.get("device_type"), f"bindings[{index}].device_type"
        ).lower()
        binding["action"] = _required_string(binding.get("action"), f"bindings[{index}].action")
        binding.setdefault("params_template", {})
        bindings.append(binding)
    configured_device_types = {device.device_type.lower() for device in devices}
    for binding in bindings:
        if binding["device_type"] not in configured_device_types:
            raise ConfigError(
                f"bindings 中的设备类型 {binding['device_type']} 没有对应的 devices 配置"
            )

    if attractions:
        attraction_ids = {attraction.id for attraction in attractions}
        for node in readers + devices:
            if node.attraction_id is None:
                raise ConfigError(f"多景点配置中节点 {node.name} 必须填写 attraction")
            if node.attraction_id not in attraction_ids:
                raise ConfigError(
                    f"节点 {node.name} 引用了不存在的景点：{node.attraction_id}"
                )
        binding_by_tag = {binding["tag"]: binding for binding in bindings}
        for attraction in attractions:
            attraction_readers = [
                reader for reader in readers if reader.attraction_id == attraction.id
            ]
            attraction_devices = [
                device for device in devices if device.attraction_id == attraction.id
            ]
            if not any(reader.device_type == "UHF" for reader in attraction_readers):
                raise ConfigError(f"景点 {attraction.name} 必须配置 UHF Reader")
            if "hf" in attraction.tags and not any(
                reader.device_type == "HF" for reader in attraction_readers
            ):
                raise ConfigError(f"景点 {attraction.name} 包含 HF 标签，但未配置 HF Reader")
            if "hf" not in attraction.tags and any(
                reader.device_type == "HF" for reader in attraction_readers
            ):
                raise ConfigError(f"景点 {attraction.name} 配置了 HF Reader，但 tags 中没有 hf")
            hf_purposes = [
                reader.hf_purpose
                for reader in attraction_readers
                if reader.device_type == "HF"
            ]
            if len(hf_purposes) != len(set(hf_purposes)):
                raise ConfigError(f"景点 {attraction.name} 的同用途 HF Reader 只能配置一个")
            hf_control_devices = [
                device for device in attraction_devices
                if device.config.get("hf_control") is True
            ]
            for device in attraction_devices:
                if device.config.get("uhf_requires_hf_authorization") is not True:
                    continue
                if device.device_type != "CAMERA" or device.config.get("hf_control") is not True:
                    raise ConfigError(
                        f"景点 {attraction.name} 的 HF 授权 UHF 交互只能配置在 HF 可控相机上"
                    )
            if len(hf_control_devices) > 1:
                raise ConfigError(f"景点 {attraction.name} 最多只能配置一个 HF 可控设备")
            if hf_control_devices and "hf" not in attraction.tags:
                raise ConfigError(f"景点 {attraction.name} 未配置 HF 标签，不能声明 HF 可控设备")
            has_control_reader = any(
                reader.device_type == "HF" and reader.hf_purpose == "control"
                for reader in attraction_readers
            )
            if has_control_reader and len(hf_control_devices) != 1:
                raise ConfigError(
                    f"景点 {attraction.name} 的控制型 HF Reader 必须对应一个 HF 可控设备"
                )
            if hf_control_devices and not has_control_reader:
                raise ConfigError(
                    f"景点 {attraction.name} 声明了 HF 可控设备，但没有控制型 HF Reader"
                )
            for tag in set(attraction.tags) & {"uhf_b", "uhf_c"}:
                if binding_by_tag.get(tag) is None:
                    raise ConfigError(f"景点 {attraction.name} 使用 {tag}，但 bindings 中没有对应绑定")
                # UHF-B/UHF-C 是手环的全局交互入口。一个景点可以没有与
                # 当前绑定相匹配的固定设备，用于验证后端的无目标设备分支。
            if attraction.latitude is not None and not -90 <= attraction.latitude <= 90:
                raise ConfigError(f"景点 {attraction.name} 的 latitude 超出范围")
            if attraction.longitude is not None and not -180 <= attraction.longitude <= 180:
                raise ConfigError(f"景点 {attraction.name} 的 longitude 超出范围")
    else:
        hf_control_devices = [device for device in devices if device.config.get("hf_control") is True]
        for device in devices:
            if device.config.get("uhf_requires_hf_authorization") is not True:
                continue
            if device.device_type != "CAMERA" or device.config.get("hf_control") is not True:
                raise ConfigError("HF 授权 UHF 交互只能配置在 HF 可控相机上")
        if len(hf_control_devices) > 1:
            raise ConfigError("单点配置最多只能声明一个 HF 可控设备")
        if hf_control_devices and "HF" not in reader_types:
            raise ConfigError("未配置 HF Reader，不能声明 HF 可控设备")
        has_control_reader = any(
            reader.device_type == "HF" and reader.hf_purpose == "control"
            for reader in readers
        )
        if has_control_reader and len(hf_control_devices) != 1:
            raise ConfigError("控制型 HF Reader 必须对应一个 HF 可控设备")
        if hf_control_devices and not has_control_reader:
            raise ConfigError("声明了 HF 可控设备，但没有控制型 HF Reader")

    app_raw = raw.get("app", {})
    if not isinstance(app_raw, dict):
        raise ConfigError("配置项 app 必须是对象")
    user_id_raw = app_raw.get("user_id")
    app = AppSettings(
        token_env=str(app_raw.get("token_env", "SIM_APP_TOKEN")),
        jwt_secret_env=str(app_raw.get("jwt_secret_env", "SIM_JWT_SECRET")),
        user_id=int(user_id_raw) if user_id_raw not in (None, "") else None,
        jwt_algorithm=str(app_raw.get("jwt_algorithm", "auto")).upper(),
        token_ttl_seconds=int(app_raw.get("token_ttl_seconds", 3600)),
    )
    if app.jwt_algorithm not in {"AUTO", "HS256", "HS384", "HS512"}:
        raise ConfigError("app.jwt_algorithm 仅支持 auto、HS256、HS384、HS512")
    if app.user_id is not None and app.user_id <= 0:
        raise ConfigError("app.user_id 必须是正整数")
    if app.token_ttl_seconds <= 0:
        raise ConfigError("app.token_ttl_seconds 必须大于 0")

    ui_raw = raw.get("ui", {})
    if not isinstance(ui_raw, dict):
        raise ConfigError("配置项 ui 必须是对象")
    ui = UiSettings(
        eyebrow=str(ui_raw.get("eyebrow", UiSettings.eyebrow)),
        title=str(ui_raw.get("title", UiSettings.title)),
        description=str(ui_raw.get("description", UiSettings.description)),
        guide_title=str(ui_raw.get("guide_title", UiSettings.guide_title)),
        guide_image_path=_resolve_media_path(
            ui_raw.get("guide_image"), config_path.parent, "ui.guide_image"
        ),
    )

    provision = raw.get("provision", {})
    scenario = raw.get("scenario", {})
    if not isinstance(provision, dict) or not isinstance(scenario, dict):
        raise ConfigError("provision 与 scenario 必须是对象")
    historical_spots = provision.get("historical_reconstruction_spots", [])
    if not isinstance(historical_spots, list):
        raise ConfigError("provision.historical_reconstruction_spots 必须是数组")
    attraction_ids = {attraction.id for attraction in attractions}
    configured_historical_attractions: set[str] = set()
    for index, item in enumerate(historical_spots):
        prefix = f"provision.historical_reconstruction_spots[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{prefix} 必须是对象")
        attraction_id = _required_string(
            item.get("attraction"), f"{prefix}.attraction"
        ).lower()
        if attraction_id not in attraction_ids:
            raise ConfigError(f"{prefix}.attraction 引用了不存在的景点：{attraction_id}")
        if attraction_id in configured_historical_attractions:
            raise ConfigError(f"历史画面业务景点重复：{attraction_id}")
        configured_historical_attractions.add(attraction_id)
        scene_profile = _required_string(
            item.get("scene_profile"), f"{prefix}.scene_profile"
        )
        if not re.fullmatch(r"[a-z0-9_]{3,80}", scene_profile):
            raise ConfigError(f"{prefix}.scene_profile 格式不正确")
        if not isinstance(item.get("enabled", True), bool):
            raise ConfigError(f"{prefix}.enabled 必须是布尔值")
    return SimulatorConfig(
        path=config_path,
        backend=backend,
        wristband=wristband,
        readers=readers,
        devices=devices,
        attractions=attractions,
        bindings=tuple(bindings),
        ui=ui,
        app=app,
        provision=dict(provision),
        scenario=dict(scenario),
    )
