"""Protocol selection for Windows clients without changing legacy behavior.

Capabilities are a client compatibility hint, never an authorization credential.
The subscription and device checks remain enforced by the API router.
"""

from collections.abc import Mapping


def windows_vpn_compatible(node: Mapping, capabilities_header: str = "") -> bool:
    """Accept only Android-compatible transports explicitly advertised by Windows."""
    config = node.get("config")
    if not isinstance(config, Mapping):
        return False

    protocol = str(config.get("protocol") or "").casefold()
    security = str(config.get("security") or "").casefold()
    network = str(config.get("network") or "tcp").casefold()

    # Older desktop builds understand only VLESS Reality TCP/raw; preserve
    # their selection behavior even when they omit the capabilities header.
    if (protocol == "vless" and security == "reality"
            and network in ("tcp", "raw") and bool(config.get("public_key"))):
        return True

    capabilities = {
        value.strip().casefold()
        for value in str(capabilities_header or "")[:256].split(",")
    }
    if not config.get("uuid") or not config.get("server"):
        return False

    if protocol == "vless":
        if security not in ("none", "tls", "reality"):
            return False
        if security == "reality" and (
                not config.get("public_key") or not config.get("server_name")):
            return False
        if security == "tls" and "vless-tls" not in capabilities:
            return False
        if security == "none" and "vless-none" not in capabilities:
            return False
        if str(config.get("flow") or "") not in ("", "xtls-rprx-vision"):
            return False
        if network in ("tcp", "raw"):
            return security != "reality"
        if network in ("grpc", "xhttp"):
            return f"vless-{network}" in capabilities
        if network in ("ws", "websocket", "httpupgrade"):
            if security == "reality":
                return False
            required = "vless-ws" if network in ("ws", "websocket") else "vless-httpupgrade"
            return required in capabilities
        return False

    if protocol in ("hysteria2", "hy2"):
        if security != "tls" or network not in ("udp", "hysteria"):
            return False
        if "hysteria2" not in capabilities:
            return False
        obfuscation = str(config.get("encryption") or "").casefold()
        return obfuscation == "" or (
            obfuscation == "salamander" and bool(config.get("flow"))
        )

    return False
