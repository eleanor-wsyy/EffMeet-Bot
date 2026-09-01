# -*- coding: utf-8 -*-
"""Windows Wi-Fi helpers for the local EffMeet device setup wizard."""

import locale
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from xml.sax.saxutils import escape


SETUP_AP_PASSWORD = "EffMeet123"
SETUP_DEVICE_URL = "http://192.168.4.1/save"


class SetupError(RuntimeError):
    pass


def _decode_output(raw):
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gb18030"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_netsh(*arguments, timeout=20):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["netsh", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Windows Wi-Fi 命令执行失败：{exc}") from exc
    output = _decode_output(result.stdout)
    if result.returncode != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())
        raise SetupError(detail or f"netsh 返回错误码 {result.returncode}")
    return output


def parse_connected_wifi(output):
    interfaces = []
    current = {}
    aliases = {
        "name": "name",
        "名称": "name",
        "state": "state",
        "状态": "state",
        "ssid": "ssid",
        "profile": "profile",
        "配置文件": "profile",
        "band": "band",
        "频带": "band",
    }
    for raw_line in output.splitlines():
        if ":" not in raw_line:
            continue
        key, value = (part.strip() for part in raw_line.split(":", 1))
        normalized = aliases.get(key.lower(), aliases.get(key))
        if normalized == "name" and current.get("name"):
            interfaces.append(current)
            current = {}
        if normalized:
            current[normalized] = value
    if current.get("name"):
        interfaces.append(current)
    if not interfaces:
        return {"name": "", "state": "", "ssid": "", "profile": "", "band": ""}
    return next((item for item in interfaces if item.get("ssid")), interfaces[0])


def connected_wifi():
    return parse_connected_wifi(run_netsh("wlan", "show", "interfaces"))


def parse_visible_networks(output):
    networks = []
    current = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.upper().startswith("SSID ") and ":" in line:
            if current:
                current["bands"] = sorted(current["bands"])
                networks.append(current)
            current = {
                "ssid": line.split(":", 1)[1].strip(),
                "authentication": "",
                "bands": set(),
            }
            continue
        if current is None or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        lowered = key.lower()
        if lowered in {"authentication", "身份验证"}:
            current["authentication"] = value
        elif lowered in {"band", "频带"} and value:
            current["bands"].add(value)
    if current:
        current["bands"] = sorted(current["bands"])
        networks.append(current)
    return networks


def visible_networks():
    return parse_visible_networks(run_netsh("wlan", "show", "networks", "mode=bssid"))


def validate_target_network(networks, target_ssid):
    matches = [item for item in networks if item["ssid"] == target_ssid]
    if not matches:
        return None
    authentications = " ".join(item.get("authentication", "") for item in matches).lower()
    if "enterprise" in authentications or "802.1x" in authentications:
        return "该网络需要校园/企业账号认证，ESP32 无法只用 Wi-Fi 名称和密码连接。"
    bands = {band.lower() for item in matches for band in item.get("bands", [])}
    if bands and not any("2.4" in band for band in bands):
        return "当前只发现该 Wi-Fi 的 5/6 GHz 信号；ESP32 只能连接 2.4 GHz。"
    return None


def _profile_xml(ssid, password):
    return f"""<?xml version=\"1.0\"?>
<WLANProfile xmlns=\"http://www.microsoft.com/networking/WLAN/profile/v1\">
  <name>{escape(ssid)}</name>
  <SSIDConfig><SSID><name>{escape(ssid)}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
  <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{escape(password)}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>"""


def wait_for_ssid(ssid, timeout=20):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = connected_wifi()
        if last.get("ssid") == ssid:
            return last
        time.sleep(1)
    raise SetupError(f"连接 Wi-Fi 超时：{ssid}；当前网络：{last.get('ssid') or '未连接'}")


def connect_setup_hotspot(setup_ssid, interface_name):
    profile_path = Path(tempfile.gettempdir()) / f"effmeet-wifi-{uuid.uuid4().hex}.xml"
    try:
        profile_path.write_text(_profile_xml(setup_ssid, SETUP_AP_PASSWORD), encoding="utf-8")
        run_netsh(
            "wlan", "add", "profile", f"filename={profile_path}",
            f"interface={interface_name}", "user=current",
        )
        run_netsh(
            "wlan", "connect", f"name={setup_ssid}", f"ssid={setup_ssid}",
            f"interface={interface_name}",
        )
        return wait_for_ssid(setup_ssid)
    finally:
        profile_path.unlink(missing_ok=True)


def send_wifi_credentials(target_ssid, password, timeout=12):
    body = urllib.parse.urlencode({"ssid": target_ssid, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        SETUP_DEVICE_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise SetupError(f"机器人返回 HTTP {response.status}")
            return response.read(4096).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"无法把 Wi-Fi 配置发送给机器人：{exc}") from exc


def restore_original_wifi(original):
    interface_name = original.get("name")
    original_ssid = original.get("ssid")
    original_profile = original.get("profile")
    if not interface_name:
        raise SetupError("未识别到可恢复的 Windows Wi-Fi 网卡。")
    if original_profile and original_ssid:
        run_netsh(
            "wlan", "connect", f"name={original_profile}", f"ssid={original_ssid}",
            f"interface={interface_name}",
        )
        return wait_for_ssid(original_ssid, timeout=30)
    run_netsh("wlan", "disconnect", f"interface={interface_name}")
    return connected_wifi()


def remove_setup_profile(setup_ssid, interface_name):
    try:
        run_netsh(
            "wlan", "delete", "profile", f"name={setup_ssid}",
            f"interface={interface_name}",
        )
    except SetupError:
        pass


def internet_available(host="broker.emqx.io", port=1883, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
