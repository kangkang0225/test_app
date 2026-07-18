from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .clients import ApiError
from .config import ConfigError, SimulatorConfig, load_config
from .provision import AdminProvisioner
from .runtime import SimulatorRuntime


HELP_TEXT = """
可用命令：
  status                         查看所有模拟节点状态
  dwell [秒数]                   模拟 UHF-A 在景点停留（默认 35 秒）
  uhf-a | uhf-b | uhf-c | hf     上报对应腕带标签
  tag <标签名>                   上报指定标签
  ack <设备名或ID> <模式>         设置 success/failed/timeout/rejected/none
  offline <节点名或ID>            模拟设备掉线
  online <节点名或ID>             重新连接并注册心跳
  upload <相机名或ID> <命令ID>     手动上传模拟照片
  control-status                 查询当前 HF 控制权限
  control <设备ID> <动作> [JSON]   通过 App API 下发控制命令
  scenario                       执行完整默认场景
  logs [条数]                    显示最近日志
  help                           显示本帮助
  quit | exit                    退出
""".strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_simulator",
        description="RFID 手环、读写器和边缘控制设备端到端模拟器",
    )
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    parser.add_argument("--verbose", action="store_true", help="打印完整收发 JSON")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("validate", help="仅校验配置，不连接后端")

    provision = subparsers.add_parser("provision", help="通过 Admin API 幂等创建测试数据")
    provision.add_argument("--username", help="管理员用户名；默认读取配置指定的环境变量")

    doctor = subparsers.add_parser("doctor", help="连接所有 TCP 节点并检查心跳")
    doctor.add_argument("--admin-audit", action="store_true", help="同时核对后台测试数据")
    doctor.add_argument("--username", help="执行后台核对时使用的管理员用户名")

    subparsers.add_parser("scenario", help="连接后端并执行默认完整场景")

    full = subparsers.add_parser("full", help="一键初始化测试数据并执行完整场景")
    full.add_argument("--username", help="管理员用户名；默认读取配置指定的环境变量")

    web = subparsers.add_parser("web", help="启动成都多景点可视化控制台")
    web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    web.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    web.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    web.add_argument("--provision", action="store_true", help="启动界面前先幂等初始化测试数据")
    web.add_argument("--username", help="初始化时使用的管理员用户名")

    subparsers.add_parser("interactive", help="进入交互控制台（默认）")
    return parser


def _admin_credentials(config: SimulatorConfig, username_arg: str | None) -> tuple[str, str]:
    username_env = str(config.provision.get("admin_username_env", "SIM_ADMIN_USERNAME"))
    password_env = str(config.provision.get("admin_password_env", "SIM_ADMIN_PASSWORD"))
    username = username_arg or os.environ.get(username_env) or str(
        config.provision.get("admin_username", "admin")
    )
    password = os.environ.get(password_env)
    if not password:
        if not sys.stdin.isatty():
            raise ConfigError(f"非交互环境下必须设置管理员密码环境变量 {password_env}")
        password = getpass.getpass(f"管理员 {username} 密码：")
    return username, password


def _provision(config: SimulatorConfig, username_arg: str | None, *, verbose: bool) -> None:
    username, password = _admin_credentials(config, username_arg)
    runtime = SimulatorRuntime(config, verbose=verbose)
    try:
        result = AdminProvisioner(config, username, password, runtime.logs.add).provision()
    finally:
        runtime.close()
    print("\n初始化完成：")
    print(f"  景区数据库 ID: {result.scenic_area_id}")
    print(f"  点位数据库 ID: {json.dumps(result.spot_ids, ensure_ascii=False)}")
    print(f"  手环数据库 ID: {result.wristband_id}")
    print(f"  绑定用户 ID: {result.user_id if result.user_id is not None else '未绑定'}")
    print(f"  标签 ID: {json.dumps(result.tag_ids, ensure_ascii=False)}")
    for action in result.actions:
        print(f"  - {action}")


def _doctor(config: SimulatorConfig, *, verbose: bool, admin_audit: bool, username: str | None) -> None:
    if admin_audit:
        admin_username, password = _admin_credentials(config, username)
        audit_runtime = SimulatorRuntime(config, verbose=verbose)
        try:
            audit = AdminProvisioner(
                config, admin_username, password, audit_runtime.logs.add
            ).audit()
        finally:
            audit_runtime.close()
        if not audit["ok"]:
            raise RuntimeError("后台缺少测试数据：" + ", ".join(audit["missing"]))
        print("后台测试数据：正常")
    runtime = SimulatorRuntime(config, verbose=verbose)
    try:
        runtime.connect_all()
        _print_status(runtime)
        print("TCP 心跳检查：所有节点正常")
    finally:
        runtime.close()


def _scenario(config: SimulatorConfig, *, verbose: bool) -> None:
    runtime = SimulatorRuntime(config, verbose=verbose)
    try:
        runtime.connect_all()
        runtime.run_default_scenario()
        time.sleep(0.2)
        _print_status(runtime)
    finally:
        runtime.close()


def _interactive(config: SimulatorConfig, *, verbose: bool) -> None:
    runtime = SimulatorRuntime(config, verbose=verbose)
    try:
        print("正在连接模拟节点……")
        runtime.connect_all()
        print(HELP_TEXT)
        while True:
            try:
                raw = input("\nrfid-sim> ").strip()
            except EOFError:
                break
            if not raw:
                continue
            try:
                parts = shlex.split(raw)
                command = parts[0].lower()
                args = parts[1:]
                if command in {"quit", "exit"}:
                    break
                if command == "help":
                    print(HELP_TEXT)
                elif command == "status":
                    _print_status(runtime)
                elif command == "dwell":
                    runtime.simulate_dwell(float(args[0]) if args else 35)
                elif command in {"uhf-a", "uhf-b", "uhf-c", "hf"}:
                    runtime.send_tag(command)
                elif command == "tag" and len(args) == 1:
                    runtime.send_tag(args[0])
                elif command == "ack" and len(args) == 2:
                    runtime.set_ack_mode(args[0], args[1])
                elif command == "offline" and len(args) == 1:
                    runtime.disconnect(args[0])
                elif command == "online" and len(args) == 1:
                    runtime.reconnect(args[0])
                elif command == "upload" and len(args) == 2:
                    command_id = int(args[1])
                    result = runtime.upload_camera_image(args[0], command_id)
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                elif command == "control-status" and not args:
                    print(json.dumps(runtime.current_control(), ensure_ascii=False, indent=2, default=str))
                elif command == "control" and len(args) >= 2:
                    params: dict[str, Any] = json.loads(args[2]) if len(args) >= 3 else {}
                    if not isinstance(params, dict):
                        raise ValueError("control 的 JSON 参数必须是对象")
                    result = runtime.send_control_command(args[0], args[1], params)
                    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                elif command == "scenario" and not args:
                    runtime.run_default_scenario()
                elif command == "logs" and len(args) <= 1:
                    _print_logs(runtime, int(args[0]) if args else 30)
                else:
                    print("命令或参数不正确；输入 help 查看用法")
            except (ApiError, ConfigError, ConnectionError, KeyError, RuntimeError, TimeoutError, ValueError) as exc:
                print(f"错误：{exc}")
    finally:
        runtime.close()
        print("模拟器已退出，所有 TCP 连接已关闭。")


def _print_status(runtime: SimulatorRuntime) -> None:
    rows = runtime.status_rows()
    headers = ("name", "device_id", "role", "type", "status", "ack")
    widths = {header: max(len(header), *(len(str(row[header])) for row in rows)) for header in headers}
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(str(row[header]).ljust(widths[header]) for header in headers))


def _print_logs(runtime: SimulatorRuntime, limit: int) -> None:
    for entry in runtime.logs.recent(limit):
        detail = "" if entry.data is None else " " + json.dumps(entry.data, ensure_ascii=False, default=str)
        print(f"[{entry.timestamp}] {entry.level:<5} {entry.source}: {entry.message}{detail}")


def _config_summary(config: SimulatorConfig) -> None:
    print(f"配置有效：{config.path}")
    print(f"HTTP: {config.backend.http_base}")
    print(f"TCP:  {config.backend.tcp_host}:{config.backend.tcp_port}")
    print(f"手环: {config.wristband['qr_code']}")
    print(f"读写器: {len(config.readers)}；控制设备: {len(config.devices)}；交互绑定: {len(config.bindings)}")
    if config.attractions:
        print(f"景点: {len(config.attractions)}（{ '、'.join(item.name for item in config.attractions) }）")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
        command = args.command or "interactive"
        if command == "validate":
            _config_summary(config)
        elif command == "provision":
            _provision(config, args.username, verbose=args.verbose)
        elif command == "doctor":
            _doctor(
                config,
                verbose=args.verbose,
                admin_audit=args.admin_audit,
                username=args.username,
            )
        elif command == "scenario":
            _scenario(config, verbose=args.verbose)
        elif command == "full":
            _provision(config, args.username, verbose=args.verbose)
            _scenario(config, verbose=args.verbose)
        elif command == "web":
            if args.provision:
                _provision(config, args.username, verbose=args.verbose)
            from .web import serve_web

            serve_web(
                config,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
                verbose=args.verbose,
            )
        elif command == "interactive":
            _interactive(config, verbose=args.verbose)
        else:
            parser.error(f"未知命令：{command}")
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except (ApiError, ConfigError, ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
