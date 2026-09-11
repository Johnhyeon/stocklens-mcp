"""SecretPayload 테스트 (1.0 Task 3).

비밀값 운반체. schema 에 없는 필드를 거부하고, 어떤 출력 경로로도
원문·일부·길이·hash 를 드러내지 않는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import (
    SecretPayload,
    SecretValidationError,
)

_KIS_SCHEMA = None


def _kis_schema():
    global _KIS_SCHEMA
    if _KIS_SCHEMA is None:
        _KIS_SCHEMA = registry.require("kis").credential_schema
    return _KIS_SCHEMA


_SECRET = "PSVERYSECRETVALUE123"


def _payload(**overrides) -> SecretPayload:
    values = {"app_key": _SECRET, "app_secret": "s3cr3t-app-secret"}
    values.update(overrides)
    return SecretPayload.from_schema(_kis_schema(), values)


class SecretPayloadValidationTests(unittest.TestCase):
    def test_exact_schema_fields_are_required(self):
        with self.assertRaises(SecretValidationError):
            SecretPayload.from_schema(_kis_schema(), {"app_key": "x"})

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(SecretValidationError):
            SecretPayload.from_schema(_kis_schema(), {
                "app_key": "x", "app_secret": "y", "account_no": "z"})

    def test_empty_and_non_string_values_are_rejected(self):
        for bad in ("", "   ", None, 123):
            with self.assertRaises(SecretValidationError):
                SecretPayload.from_schema(_kis_schema(), {
                    "app_key": bad, "app_secret": "y"})

    def test_overlong_value_is_rejected_without_echo(self):
        try:
            SecretPayload.from_schema(_kis_schema(), {
                "app_key": "A" * 5000, "app_secret": "y"})
        except SecretValidationError as exc:
            self.assertNotIn("A" * 10, str(exc))
        else:
            self.fail("max_length 초과가 거부되지 않았습니다")

    def test_error_messages_never_contain_values(self):
        try:
            SecretPayload.from_schema(_kis_schema(), {
                "app_key": _SECRET, "app_secret": "y", "extra": _SECRET})
        except SecretValidationError as exc:
            self.assertNotIn(_SECRET, str(exc))
            self.assertNotIn(_SECRET, repr(exc))
        else:
            self.fail("unknown 필드가 거부되지 않았습니다")


class SecretPayloadLeakTests(unittest.TestCase):
    def test_repr_and_str_are_masked(self):
        payload = _payload()
        self.assertEqual(repr(payload), "SecretPayload(***)")
        self.assertEqual(str(payload), "SecretPayload(***)")
        self.assertNotIn(_SECRET, f"{payload}")
        self.assertNotIn(_SECRET, f"{payload!r}")

    def test_not_json_serializable(self):
        with self.assertRaises(TypeError):
            json.dumps(_payload())

    def test_not_a_dict_and_not_iterable(self):
        payload = _payload()
        self.assertNotIsInstance(payload, dict)
        with self.assertRaises(TypeError):
            iter(payload)

    def test_vars_and_dataclass_dumps_do_not_leak(self):
        payload = _payload()
        leaked = []
        try:
            leaked.append(str(vars(payload)))
        except TypeError:
            pass
        for text in leaked:
            self.assertNotIn(_SECRET, text)

    def test_explicit_get_returns_value(self):
        payload = _payload()
        self.assertEqual(payload.get("app_key"), _SECRET)
        with self.assertRaises(SecretValidationError):
            payload.get("nonexistent")

    def test_field_names_are_exposed_without_values(self):
        payload = _payload()
        self.assertEqual(payload.field_names(), ("app_key", "app_secret"))


if __name__ == "__main__":
    unittest.main()
