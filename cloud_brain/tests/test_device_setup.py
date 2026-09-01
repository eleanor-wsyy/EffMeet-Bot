# -*- coding: utf-8 -*-
from unittest.mock import patch

import main_brain as brain
import windows_setup


INTERFACES = """
    Name                   : WLAN
    State                  : connected
    SSID                   : Lab_24G
    Band                   : 2.4 GHz
    Profile                : Lab_24G
"""

NETWORKS = """
SSID 1 : EffMeet-Setup-12AB
    Authentication          : WPA2-Personal
         Band               : 2.4 GHz
SSID 2 : Lab_24G
    Authentication          : WPA2-Personal
         Band               : 2.4 GHz
SSID 3 : Campus
    Authentication          : WPA2-Enterprise
         Band               : 2.4 GHz
SSID 4 : Only5G
    Authentication          : WPA2-Personal
         Band               : 5 GHz
"""


def test_windows_wifi_parsers_and_validation():
    connected = windows_setup.parse_connected_wifi(INTERFACES)
    assert connected["name"] == "WLAN"
    assert connected["ssid"] == "Lab_24G"
    assert connected["profile"] == "Lab_24G"

    networks = windows_setup.parse_visible_networks(NETWORKS)
    assert [item["ssid"] for item in networks] == [
        "EffMeet-Setup-12AB", "Lab_24G", "Campus", "Only5G"
    ]
    assert windows_setup.validate_target_network(networks, "Lab_24G") is None
    assert "账号认证" in windows_setup.validate_target_network(networks, "Campus")
    assert "5/6 GHz" in windows_setup.validate_target_network(networks, "Only5G")


def test_provisioning_success_restores_network_without_storing_password():
    original_state = brain._setup_state_snapshot()
    original_mqtt = brain._mqtt_connected.is_set()
    original_robot = brain._robot_online.is_set()

    def send_credentials(_ssid, _password):
        brain._robot_online.set()
        return "ok"

    brain._mqtt_connected.set()
    try:
        with patch.object(windows_setup, "connected_wifi", return_value={
            "name": "WLAN", "ssid": "Original", "profile": "Original", "band": "5 GHz"
        }), patch.object(windows_setup, "connect_setup_hotspot", return_value={
            "name": "WLAN", "ssid": "EffMeet-Setup-12AB"
        }), patch.object(windows_setup, "send_wifi_credentials", side_effect=send_credentials), \
                patch.object(windows_setup, "restore_original_wifi", return_value={
                    "name": "WLAN", "ssid": "Original", "profile": "Original"
                }), patch.object(windows_setup, "remove_setup_profile"), \
                patch.object(brain, "_connectivity_snapshot", return_value={} ), \
                patch.object(windows_setup, "internet_available", return_value=True), \
                patch.object(brain.time, "sleep", return_value=None):
            brain._provision_robot_wifi("EffMeet-Setup-12AB", "Lab_24G", "secret-password")

        state = brain._setup_state_snapshot()
        assert state["state"] == "success"
        assert state["current_ssid"] == "Original"
        assert "secret-password" not in repr(state)
    finally:
        with brain._setup_state_lock:
            brain._setup_state.clear()
            brain._setup_state.update(original_state)
        if original_mqtt:
            brain._mqtt_connected.set()
        else:
            brain._mqtt_connected.clear()
        if original_robot:
            brain._robot_online.set()
        else:
            brain._robot_online.clear()


if __name__ == "__main__":
    test_windows_wifi_parsers_and_validation()
    test_provisioning_success_restores_network_without_storing_password()
    print("Device setup wizard tests passed.")
