"""Regression coverage for version-gated Windows session selection."""

import unittest

from desktop_vpn_capabilities import windows_vpn_compatible


BASE = {
    "server": "vpn.example.com",
    "server_name": "www.example.com",
    "port": 443,
    "uuid": "11111111-1111-4111-8111-111111111111",
    "security": "reality",
    "network": "tcp",
    "protocol": "vless",
    "public_key": "a-valid-base64url-reality-key",
}


def node(**changes):
    return {"config": {**BASE, **changes}}


class WindowsVpnCapabilitiesTests(unittest.TestCase):
    def test_legacy_clients_stay_limited_to_reality_tcp(self):
        self.assertTrue(windows_vpn_compatible(node()))
        self.assertTrue(windows_vpn_compatible(node(network="raw")))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp")))
        self.assertFalse(windows_vpn_compatible(node(security="tls")))
        self.assertFalse(windows_vpn_compatible(node(protocol="hysteria2", security="tls", network="udp")))

    def test_new_clients_can_choose_xhttp_and_tls(self):
        capabilities = "hysteria2,vless-xhttp,vless-tls"
        self.assertTrue(windows_vpn_compatible(node(network="xhttp"), capabilities))
        self.assertTrue(windows_vpn_compatible(node(security="tls", network="xhttp", public_key=""), capabilities))
        self.assertTrue(windows_vpn_compatible(node(security="tls", public_key=""), capabilities))
        self.assertFalse(windows_vpn_compatible(node(security="reality", public_key="", network="xhttp"), capabilities))
        self.assertFalse(windows_vpn_compatible(node(network="ws"), capabilities))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp", flow="unsupported"), capabilities))

    def test_hysteria_requires_valid_transport_authentication_and_obfuscation(self):
        capabilities = "hysteria2"
        hy2 = node(protocol="hysteria2", security="tls", network="udp", uuid="auth-token")
        self.assertTrue(windows_vpn_compatible(hy2, capabilities))
        self.assertTrue(windows_vpn_compatible(node(
            protocol="hy2", security="tls", network="hysteria", uuid="auth-token",
            encryption="salamander", flow="obfuscation-password",
        ), capabilities))
        self.assertFalse(windows_vpn_compatible(node(
            protocol="hysteria2", security="reality", network="udp",
        ), capabilities))
        self.assertFalse(windows_vpn_compatible(node(
            protocol="hysteria2", security="tls", network="tcp",
        ), capabilities))
        self.assertFalse(windows_vpn_compatible(node(
            protocol="hysteria2", security="tls", network="udp",
            encryption="salamander", flow="",
        ), capabilities))
        self.assertFalse(windows_vpn_compatible(node(
            protocol="hysteria2", security="tls", network="udp", encryption="unknown",
        ), capabilities))

    def test_windows_negotiates_all_vless_transports_that_android_generator_accepts(self):
        header = "hysteria2,vless-xhttp,vless-tls,vless-grpc,vless-ws,vless-httpupgrade,vless-none"
        for network, security in (
            ("grpc", "reality"),
            ("grpc", "tls"),
            ("ws", "tls"),
            ("websocket", "none"),
            ("httpupgrade", "tls"),
            ("httpupgrade", "none"),
            ("tcp", "none"),
            ("raw", "none"),
        ):
            with self.subTest(network=network, security=security):
                self.assertTrue(windows_vpn_compatible(node(
                    network=network, security=security,
                    public_key=BASE["public_key"] if security == "reality" else "",
                ), header))
        self.assertFalse(windows_vpn_compatible(node(network="ws"), header))
        self.assertFalse(windows_vpn_compatible(node(network="httpupgrade"), header))
        self.assertFalse(windows_vpn_compatible(node(network="grpc"), "vless-tls"))
        self.assertFalse(windows_vpn_compatible(node(network="ws", security="none"), "vless-ws"))
        self.assertFalse(windows_vpn_compatible(node(network="none"), header))

    def test_tls_or_none_can_have_no_sni_but_reality_cannot(self):
        header = "vless-tls,vless-ws,vless-none"
        self.assertTrue(windows_vpn_compatible(
            node(network="ws", security="tls", server_name=""), header))
        self.assertTrue(windows_vpn_compatible(
            node(network="ws", security="none", server_name=""), header))
        self.assertFalse(windows_vpn_compatible(
            node(network="grpc", security="reality", server_name=""),
            "vless-grpc"))

    def test_capabilities_are_only_selection_hints(self):
        extended = "hysteria2,vless-xhttp,vless-tls"
        self.assertFalse(windows_vpn_compatible({"config": None}, extended))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp", uuid=None), extended))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp", server=None), extended))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp", server_name=None), extended))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp"), "hysteria2"))
        self.assertFalse(windows_vpn_compatible(node(network="xhttp"), "unknown"))


if __name__ == "__main__":
    unittest.main()
