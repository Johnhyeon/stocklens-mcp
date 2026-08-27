"""Provider Registry 테스트 (1.0 Task 3).

증권사 정의는 코드에 고정된 불변 descriptor 다. 외부 설정·사용자 입력으로
공급자를 만들 수 없고, 시세 외 endpoint 는 어떤 descriptor 에도 없다.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.provider_registry import (
    UnknownProviderError,
    registry,
)

# 주문·계좌·잔고 계열 경로가 허용 목록에 존재하면 안 된다.
_FORBIDDEN_PATH_KEYWORDS = (
    "order", "ordr", "account", "acnt", "balance", "asset",
    "holding", "trading-order", "buy", "sell", "margin", "psbl",
)


class RegistryContractTests(unittest.TestCase):
    def test_registry_contains_exactly_supported_brokers(self):
        self.assertEqual(registry.ids(), ("kis", "kiwoom", "toss"))

    def test_unknown_provider_is_rejected(self):
        for bad in ("evil", "naver", "yahoo", "", "KIS", "kis "):
            with self.assertRaises(UnknownProviderError):
                registry.require(bad)

    def test_descriptor_hosts_and_paths_are_immutable(self):
        for provider_id in registry.ids():
            desc = registry.require(provider_id)
            self.assertIsInstance(desc.allowed_hosts, tuple)
            self.assertIsInstance(desc.allowed_paths, tuple)
            self.assertIsInstance(desc.supported_profiles, tuple)
            self.assertIsInstance(desc.credential_schema, tuple)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                desc.allowed_hosts = ("attacker.example",)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                desc.allowed_paths = ("/etc/passwd",)

    def test_each_descriptor_has_only_market_data_paths(self):
        for provider_id in registry.ids():
            desc = registry.require(provider_id)
            self.assertTrue(desc.allowed_hosts)
            self.assertTrue(desc.allowed_paths)
            for path in desc.allowed_paths:
                self.assertTrue(path.startswith("/"), path)
                self.assertNotIn("*", path)
                self.assertNotIn("..", path)
                lowered = path.lower()
                for keyword in _FORBIDDEN_PATH_KEYWORDS:
                    self.assertNotIn(
                        keyword, lowered,
                        f"{provider_id} 허용 경로에 시세 외 endpoint: {path}")
            for host in desc.allowed_hosts:
                self.assertNotIn("/", host)
                self.assertNotIn("*", host)

    def test_toss_has_real_profile_only_until_certified(self):
        self.assertEqual(registry.require("toss").supported_profiles,
                         ("real",))

    def test_kis_and_kiwoom_have_real_and_demo_profiles(self):
        self.assertEqual(registry.require("kis").supported_profiles,
                         ("real", "demo"))
        self.assertEqual(registry.require("kiwoom").supported_profiles,
                         ("real", "demo"))

    def test_kis_descriptor_matches_existing_client_contract(self):
        # 기존 KisClient 상수와 어긋나면 레지스트리 이관 때 연결이 깨진다.
        desc = registry.require("kis")
        self.assertIn("openapi.koreainvestment.com:9443", desc.allowed_hosts)
        self.assertIn("openapivts.koreainvestment.com:29443",
                      desc.allowed_hosts)
        self.assertIn("/oauth2/tokenP", desc.allowed_paths)
        self.assertIn(
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
            desc.allowed_paths)
        self.assertIn(
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
            desc.allowed_paths)

    def test_credential_schema_has_no_account_fields(self):
        for provider_id in registry.ids():
            desc = registry.require(provider_id)
            names = {f.name for f in desc.credential_schema}
            self.assertTrue(names)
            for banned in ("account", "account_no", "cano", "acnt_no",
                           "hts_id", "password", "pin"):
                self.assertNotIn(banned, names)

    def test_describe_public_is_serializable_without_factories(self):
        public = registry.describe_public()
        text = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("factory", text)
        self.assertNotIn("allowed_hosts", text)
        by_id = {entry["provider_id"]: entry for entry in public}
        self.assertEqual(set(by_id), {"kis", "kiwoom", "toss"})
        for entry in public:
            self.assertTrue(entry["display_name"])
            self.assertTrue(entry["signup_url"].startswith("https://"))
            self.assertTrue(entry["docs_url"].startswith("https://"))
            for field in entry["credential_fields"]:
                self.assertIn("name", field)
                self.assertIn("label", field)
                self.assertNotIn("value", field)
        self.assertEqual(
            [f["name"] for f in by_id["kis"]["credential_fields"]],
            ["app_key", "app_secret"])
        self.assertEqual(
            [f["name"] for f in by_id["kiwoom"]["credential_fields"]],
            ["app_key", "secret_key"])
        self.assertEqual(
            [f["name"] for f in by_id["toss"]["credential_fields"]],
            ["client_id", "client_secret"])


if __name__ == "__main__":
    unittest.main()
