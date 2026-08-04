# -*- coding: utf-8 -*-
"""机器人现场连续稳定性测试：表情预检 + 默认 5 次完整往返。"""

import argparse
import json
import queue
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt


if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)


MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC_CONTROL = "esp32s3/control"
MQTT_TOPIC_STATUS = "esp32s3/status"
DEFAULT_TARGETS = [1, 2, 3, 4, 1]


def parse_args():
    parser = argparse.ArgumentParser(
        description="连续测试 TFT 表情、MQTT 连接和机器人完整往返。"
    )
    parser.add_argument(
        "--targets",
        nargs="*",
        type=int,
        default=DEFAULT_TARGETS,
        help="依次测试的座位，默认：1 2 3 4 1（共 5 次）。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="每次完整往返的等待上限，默认 180 秒。",
    )
    parser.add_argument(
        "--expression-only",
        action="store_true",
        help="只轮播 4 种表情，不执行移动。",
    )
    return parser.parse_args()


def parse_fields(payload):
    fields = {}
    for part in payload.split("|")[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    return fields


def wait_for_message(messages, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = messages.get(timeout=min(1.0, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if predicate(payload):
            return payload
    raise TimeoutError(f"等待消息超时（{timeout:.0f}s）")


def main():
    args = parse_args()
    if any(target not in {1, 2, 3, 4} for target in args.targets):
        raise SystemExit("--targets 只能包含 1、2、3、4。")
    if not args.expression_only and len(args.targets) < 5:
        print("[提醒] 当前目标少于 5 次；正式验收建议至少连续测试 5 次。")

    connected = False
    messages = queue.Queue()
    results = []

    def on_connect(client, userdata, flags, rc):
        nonlocal connected
        connected = rc == 0
        if connected:
            client.subscribe(MQTT_TOPIC_STATUS)
            print(f"[MQTT] 已连接并订阅 {MQTT_TOPIC_STATUS}")
        else:
            print(f"[MQTT] 连接失败 rc={rc}")

    def on_disconnect(client, userdata, rc):
        nonlocal connected
        connected = False
        print(f"[MQTT] 断开 rc={rc}，客户端正在自动重连…")

    def on_message(client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        print(f"[接收] {payload}")
        messages.put(payload)

    client_id = f"EffMeet_Stability_{random.randint(10000, 99999)}"
    client = mqtt.Client(client_id=client_id)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(1, 15)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    try:
        deadline = time.monotonic() + 15
        while not connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if not connected:
            raise RuntimeError("15 秒内未连接 MQTT Broker。")

        print("\n=== 第一步：4 种表情预检 ===")
        print("请现场观察：专注、提醒、好奇、稳定都必须完整覆盖整块屏幕。")
        for expression in ("focus", "reminder", "curious", "stable"):
            payload = f"expr:{expression}"
            client.publish(MQTT_TOPIC_CONTROL, payload)
            ack = wait_for_message(
                messages,
                lambda item, expected=expression: (
                    item.startswith("ack|type=expression")
                    and parse_fields(item).get("expression") == expected
                ),
                timeout=15,
            )
            results.append({"kind": "expression", "command": payload, "reply": ack})
            print(f"[通过] {expression} 已完整执行；观察 5 秒。")
            time.sleep(5)

        if args.expression_only:
            print("\n[完成] 4 种表情预检全部通过。")
            return

        print("\n=== 第二步：连续完整往返 ===")
        occurrences = Counter()
        for index, target in enumerate(args.targets, start=1):
            occurrences[target] += 1
            expression = "reminder" if occurrences[target] == 1 else "curious"
            command = f"move:{target}:{expression}"
            print(f"\n[{index}/{len(args.targets)}] 发送 {command}")
            client.publish(MQTT_TOPIC_CONTROL, command)

            reply = wait_for_message(
                messages,
                lambda item, expected_target=target, expected_expression=expression: (
                    item.startswith("error|")
                    or (
                        item.startswith("done|")
                        and parse_fields(item).get("target") == str(expected_target)
                        and parse_fields(item).get("expression") == expected_expression
                    )
                ),
                timeout=args.timeout,
            )
            if reply.startswith("error|"):
                raise RuntimeError(f"机器人主动报告故障：{reply}")

            results.append({"kind": "movement", "command": command, "reply": reply})
            print(f"[通过] 第 {index} 次往返完成，MQTT 未丢失 done。")

        print(f"\n[验收通过] 连续 {len(args.targets)} 次完整往返全部完成。")
    finally:
        report_dir = Path(__file__).resolve().parent / "data" / "hardware_tests"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"stability_{datetime.now():%Y%m%d-%H%M%S}.json"
        report_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        client.loop_stop()
        client.disconnect()
        print(f"[报告] {report_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[测试失败] {exc}")
        sys.exit(1)
