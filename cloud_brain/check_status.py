# -*- coding: utf-8 -*-
"""
实时查看各麦克风发言时长（调用云端 Flask API）
用法：python check_status.py
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import urllib.request
import json
import time

URL = "http://127.0.0.1:5000/api/get_meeting_data"

print("=== EffMeet 实时发言统计 ===")
print("每3秒刷新一次，Ctrl+C 退出\n")

while True:
    try:
        with urllib.request.urlopen(URL, timeout=3) as resp:
            data = json.loads(resp.read())
        times = data["current_speaking_times"]
        total = sum(times.values())
        avg   = total / 4.0 if total > 0 else 0

        print(f"\r[{time.strftime('%H:%M:%S')}]  ", end="")
        for node, t in sorted(times.items()):
            bar = "#" * int(t / max(total, 1) * 20)
            flag = " <-- 偏少!" if t < avg * 0.5 and total > 5 else ""
            print(f"{node}: {t:5.1f}s {bar}{flag}  ", end="")
        print("", flush=True)

    except Exception as e:
        print(f"\r[{time.strftime('%H:%M:%S')}] 等待云端启动... ({e})", end="", flush=True)

    time.sleep(3)
