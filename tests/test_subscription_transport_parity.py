"""Session parser must preserve Android's server-owned transport settings.

Regression fixtures are synthetic URIs, never production credentials.
"""
import os
import secrets
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_subscription_transport_parity.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

from api_router import parse_single_node_line

UUID = "11111111-1111-4111-8111-111111111111"


class SubscriptionTransportParityTests(unittest.TestCase):
    def node(self, url):
        result = parse_single_node_line(url, 0)
        self.assertIsNotNone(result, url.split("?")[0].split("@")[-1])
        return result["config"]

    def test_reality_grpc_service_name_and_multi_mode_reach_windows(self):
        node = self.node(
            f"vless://{UUID}@vpn.example.com:16287?"
            "security=reality&type=grpc&serviceName=grpc-main&mode=multi"
            "&sni=cdn.example.com&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "#Reality"
        )
        self.assertEqual(node["network"], "grpc")
        self.assertEqual(node["transport_path"], "grpc-main")
        self.assertEqual(node["transport_mode"], "multi")
        self.assertEqual(node["flow"], "")
        self.assertEqual(node["security"], "reality")

    def test_websocket_and_httpupgrade_keep_host_and_path(self):
        for scheme in ("ws", "httpupgrade"):
            with self.subTest(scheme=scheme):
                node = self.node(
                    f"vless://{UUID}@vpn.example.com:443?security=tls"
                    f"&type={scheme}&path=%2Fapi%2Fv1&host=cdn.example.com"
                    "&fp=firefox&alpn=h2%2Chttp%2F1.1#TLS"
                )
                self.assertEqual(node["network"], scheme)
                self.assertEqual(node["transport_path"], "/api/v1")
                self.assertEqual(node["transport_host"], "cdn.example.com")
                self.assertEqual(node["alpn"], "h2,http/1.1")
                self.assertEqual(node["fingerprint"], "firefox")
                self.assertEqual(node["flow"], "")

    def test_tls_is_android_default_without_reality_public_key(self):
        node = self.node(f"vless://{UUID}@vpn.example.com:443?type=tcp#TLS")
        self.assertEqual(node["security"], "tls")
        self.assertEqual(node["flow"], "")

    def test_hysteria_salamander_alpn_and_fingerprint_survive(self):
        node = self.node(
            "hysteria2://subscription-auth@vpn.example.com:59763?"
            "sni=cdn.example.com&obfs=salamander&obfs-password=secret-pass"
            "&fp=chrome&alpn=h3#Hysteria"
        )
        self.assertEqual(node["network"], "udp")
        self.assertEqual(node["encryption"], "salamander")
        self.assertEqual(node["flow"], "secret-pass")
        self.assertEqual(node["fingerprint"], "chrome")
        self.assertEqual(node["alpn"], "h3")

    def test_hysteria_percent_encoded_password_is_decoded_before_xray_auth(self):
        node = self.node(
            "hysteria2://pa%3Ass%40word@vpn.example.com:59763?"
            "sni=cdn.example.com#Hysteria"
        )
        self.assertEqual(node["uuid"], "pa:ss@word")

    def test_ipv6_authority_is_parsed_without_fake_server_address(self):
        node = self.node(
            f"vless://{UUID}@[2001:db8::42]:443?"
            "security=tls&type=grpc&serviceName=upstream#IPv6"
        )
        self.assertEqual(node["server"], "2001:db8::42")
        self.assertEqual(node["port"], 443)


if __name__ == "__main__":
    unittest.main()
