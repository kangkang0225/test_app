# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "qrcode[pil]>=8.0",
#   "zxing-cpp>=2.2.0",
# ]
# ///

"""Generate and verify wristband QR images for local integration testing."""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime
from pathlib import Path

import qrcode
import zxingcpp
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M


def unique_hex(byte_count: int, used_values: set[str]) -> str:
    while True:
        value = secrets.token_hex(byte_count).upper()
        if value not in used_values:
            used_values.add(value)
            return value


def make_payload(
    index: int, batch_code: str, used_values: set[str]
) -> dict[str, object]:
    return {
        "version": 1,
        "type": "wristband",
        "uid": f"WB-TEST-{batch_code}-{index:03d}",
        "qr_code": f"QR-{unique_hex(16, used_values)}",
        "uhf_a_uid": unique_hex(8, used_values),
        "uhf_b_uid": unique_hex(8, used_values),
        "uhf_c_uid": unique_hex(8, used_values),
        "hf_uid": unique_hex(8, used_values),
    }


def write_qr(payload: dict[str, object], image_path: Path) -> str:
    qr_text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_text)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(image_path)
    return qr_text


def verify_qr(image_path: Path, expected_text: str) -> None:
    with Image.open(image_path) as image:
        result = zxingcpp.read_barcode(image)
    if result is None:
        raise RuntimeError(f"二维码无法重新解码: {image_path.name}")
    if result.text != expected_text:
        raise RuntimeError(f"二维码解码内容不一致: {image_path.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成手环测试二维码")
    parser.add_argument("--count", type=int, default=5, help="生成数量，默认 5")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "generated_qrs",
        help="输出根目录",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.count <= 100:
        raise SystemExit("--count 必须在 1 到 100 之间")

    batch_code = datetime.now().strftime("%Y%m%d%H%M%S")
    batch_name = f"batch-{batch_code}"
    batch_dir = args.output.resolve() / batch_name
    batch_dir.mkdir(parents=True, exist_ok=False)

    used_values: set[str] = set()
    payloads: list[dict[str, object]] = []

    for index in range(1, args.count + 1):
        payload = make_payload(index, batch_code, used_values)
        basename = str(payload["uid"])
        image_path = batch_dir / f"{basename}.png"
        payload_path = batch_dir / f"{basename}.json"

        qr_text = write_qr(payload, image_path)
        verify_qr(image_path, qr_text)
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payloads.append(payload)

    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(payloads, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"已生成并验证 {len(payloads)} 个手环二维码")
    print(f"输出目录: {batch_dir}")
    print(f"清单文件: {manifest_path}")


if __name__ == "__main__":
    main()
