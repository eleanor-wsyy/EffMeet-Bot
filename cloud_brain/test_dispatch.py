# -*- coding: utf-8 -*-
"""
联调测试脚本：验证动态调度逻辑
- 不需要麦克风、不需要机器人在线
- 模拟"指定发言人发言不足"，只提醒那些人
- 每次收到 done 后自动发下一个，走完通知 cycle_done

用法：
  cd cloud_brain
  python test_dispatch.py [目标编号...]

示例：
  python test_dispatch.py          # 默认模拟 3号和2号发言不足 → 干预顺序 3->2
  python test_dispatch.py 2        # 只有2号发言不足 → 只提醒2
  python test_dispatch.py 3 1      # 3号和1号发言不足 → 提醒 3->1

在 MQTTX 里：
  订阅 esp32s3/control    <- 看云端发出的数字指令
  订阅 effmeet/cycle/done <- 看本轮完成通知
  向 esp32s3/status 发 "done|dir=X" <- 模拟机器人完成回复
"""

import sys
import io
import paho.mqtt.client as mqtt
import random
import time
import threading

# 修复 Windows 终端中文乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

MQTT_BROKER           = "broker.emqx.io"
MQTT_PORT             = 1883
MQTT_TOPIC_CONTROL    = "esp32s3/control"
MQTT_TOPIC_STATUS     = "esp32s3/status"
MQTT_TOPIC_CYCLE_DONE = "effmeet/cycle/done"

# 从命令行读取目标列表，默认模拟 3号和2号发言最少
if len(sys.argv) > 1:
    try:
        ACTIVE_TARGETS = [int(x) for x in sys.argv[1:] if 1 <= int(x) <= 4]
    except ValueError:
        print("[ERROR] 参数必须是 1-4 之间的数字")
        sys.exit(1)
else:
    ACTIVE_TARGETS = [3, 2]   # 默认：3号最沉默，其次2号

_robot_busy  = False
_cycle_index = 0
_lock = threading.Lock()


def send_next(client):
    target = ACTIVE_TARGETS[_cycle_index]
    client.publish(MQTT_TOPIC_CONTROL, str(target))
    print(f"[SEND] -> esp32s3/control : '{target}'  ({_cycle_index+1}/{len(ACTIVE_TARGETS)})")
    sys.stdout.flush()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(MQTT_TOPIC_STATUS)
        print(f"[MQTT] 已连接，订阅: {MQTT_TOPIC_STATUS}")
        print( "       等待 3 秒后自动触发干预...")
    else:
        print(f"[MQTT] 连接失败 rc={rc}")
    sys.stdout.flush()


def on_message(client, userdata, msg):
    global _robot_busy, _cycle_index
    payload = msg.payload.decode("utf-8", errors="ignore").strip()
    print(f"\n[RECV] <- {msg.topic} : '{payload}'")
    sys.stdout.flush()

    if not payload.startswith("done"):
        return

    with _lock:
        _cycle_index += 1
        if _cycle_index >= len(ACTIVE_TARGETS):
            _cycle_index = 0
            _robot_busy = False
            client.publish(MQTT_TOPIC_CYCLE_DONE, "cycle_done")
            print("[DONE] 本轮干预结束！已发布 effmeet/cycle/done : 'cycle_done'")
            print("       如需再测，请重新运行脚本。")
        else:
            print(f"[NEXT] 收到完成，发送下一个...")
            send_next(client)
    sys.stdout.flush()


def trigger_cycle(client):
    """3秒后自动触发干预（模拟云端检测到发言失衡）"""
    time.sleep(3)
    global _robot_busy, _cycle_index
    with _lock:
        _cycle_index = 0
        _robot_busy = True
        print(f"\n[TRIGGER] 模拟发言失衡 -> 本轮干预目标: {ACTIVE_TARGETS}")
        send_next(client)
    sys.stdout.flush()


def main():
    client_id = "EffMeet_Test_" + str(random.randint(0, 9999))
    client = mqtt.Client(client_id=client_id)
    client.on_connect = on_connect
    client.on_message = on_message

    print("=== EffMeet 动态调度逻辑测试 ===")
    print(f"本轮干预目标: {ACTIVE_TARGETS}  (发言量从少到多)")
    print(f"连接 {MQTT_BROKER}:{MQTT_PORT} ...")
    sys.stdout.flush()

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    threading.Thread(target=trigger_cycle, args=(client,), daemon=True).start()

    print("\n" + "="*55)
    print("MQTTX 操作指南：")
    print(f"  订阅 {MQTT_TOPIC_CONTROL}    <- 看发出的指令")
    print(f"  订阅 {MQTT_TOPIC_CYCLE_DONE} <- 看本轮完成通知")
    print(f"  向  {MQTT_TOPIC_STATUS} 发 'done|dir=2' 模拟机器人完成")
    print("="*55 + "\n")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[EXIT] 测试结束")
        client.loop_stop()


if __name__ == "__main__":
    main()
