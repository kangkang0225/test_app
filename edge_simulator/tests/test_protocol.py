from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone

from edge_simulator.protocol import (
    SIMULATED_JPEG,
    build_multipart,
    command_ack_body,
    create_app_jwt,
    encode_frame,
    event_batch_body,
    hf_control_ack_body,
)


def decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


class ProtocolTests(unittest.TestCase):
    def test_frame_is_newline_delimited_utf8_json(self) -> None:
        frame = encode_frame("/测试", {"value": "中文"})
        self.assertTrue(frame.endswith(b"\n"))
        decoded = json.loads(frame)
        self.assertEqual(decoded, {"path": "/测试", "body": {"value": "中文"}})

    def test_event_batch_matches_backend_contract(self) -> None:
        moment = datetime(2026, 7, 18, 10, 20, 30, tzinfo=timezone.utc)
        body = event_batch_body("READER-1", [("TAG-1", moment, -51)], batch_id="BATCH-1")
        self.assertEqual(body["batch_id"], "BATCH-1")
        self.assertEqual(body["reader_id"], "READER-1")
        self.assertEqual(body["events"][0]["tag_id"], "TAG-1")
        self.assertEqual(body["events"][0]["rssi_strength"], -51)
        self.assertIn("+00:00", body["events"][0]["event_time"])

    def test_hf_event_can_omit_rssi(self) -> None:
        body = event_batch_body(
            "HF-1",
            [("HF-TAG-1", datetime.now().astimezone(), None)],
        )

        self.assertNotIn("rssi_strength", body["events"][0])

    def test_event_batch_can_report_an_explicit_departure(self) -> None:
        moment = datetime(2026, 7, 19, 14, 31, tzinfo=timezone.utc)
        body = event_batch_body(
            "READER-1", [("TAG-1", moment, -46)], event_type="leave"
        )

        self.assertEqual(body["events"][0]["event_type"], "leave")

    def test_command_ack_contract(self) -> None:
        body = command_ack_body("CAM-1", 42, "failed", command_type="UHF", error_code="X")
        self.assertEqual(body["command_id"], 42)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error_code"], "X")

    def test_hf_control_ack_contract_has_no_device_or_token_id(self) -> None:
        body = hf_control_ack_body("end", "success")
        self.assertEqual(body["control_event"], "end")
        self.assertEqual(body["status"], "success")
        self.assertIn("sent_at", body)
        self.assertNotIn("device_id", body)
        self.assertNotIn("token_id", body)

    def test_locally_signed_app_jwt_has_valid_hmac(self) -> None:
        secret = "x" * 32
        token = create_app_jwt(secret, 7, algorithm="auto", ttl_seconds=60, now=1000)
        header_segment, payload_segment, signature_segment = token.split(".")
        self.assertEqual(decode_segment(header_segment)["alg"], "HS256")
        payload = decode_segment(payload_segment)
        self.assertEqual(payload["sub"], "7")
        self.assertEqual(payload["token_type"], "app")
        expected = hmac.new(
            secret.encode(), f"{header_segment}.{payload_segment}".encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(signature_segment + "=" * (-len(signature_segment) % 4))
        self.assertEqual(actual, expected)

    def test_multipart_contains_required_camera_fields_and_jpeg(self) -> None:
        body, content_type = build_multipart(
            {"deviceId": "CAM-1", "commandId": "9"},
            "file",
            "test.jpg",
            SIMULATED_JPEG,
            "image/jpeg",
        )
        self.assertIn("boundary=", content_type)
        self.assertIn(b'name="deviceId"', body)
        self.assertIn(b'name="commandId"', body)
        self.assertIn(b'name="file"; filename="test.jpg"', body)
        self.assertIn(SIMULATED_JPEG, body)
        self.assertTrue(SIMULATED_JPEG.startswith(b"\xff\xd8"))
        self.assertTrue(SIMULATED_JPEG.endswith(b"\xff\xd9"))


if __name__ == "__main__":
    unittest.main()
