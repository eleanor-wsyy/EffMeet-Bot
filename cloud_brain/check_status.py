# -*- coding: utf-8 -*-
"""
实时查看各麦克风发言时长和最近一次判定结果。
用法：python check_status.py
"""

import io
import json
import os
import sys
import time
import urllib.request


if sys.platform.startswith("win"):
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass

if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )


URL = "http://127.0.0.1:5000/api/get_meeting_data"
REFRESH_SECONDS = 2


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def bar(value, total, width=24):
    if total <= 0:
        return ""
    count = int(value / total * width)
    return "#" * count


def fmt_node_table(times):
    total = sum(times.values())
    avg = total / 4.0 if total > 0 else 0.0
    lines = []
    for node, seconds in sorted(times.items()):
        percent = (seconds / total * 100.0) if total > 0 else 0.0
        flag = "偏少" if total > 5 and seconds < avg * 0.5 else ""
        lines.append(
            f"{node:<5} {seconds:>7.1f}s  {percent:>5.1f}%  "
            f"{bar(seconds, total):<24} {flag}"
        )
    return "\n".join(lines), total, avg


def fmt_recent_decision(latest_state):
    if not latest_state:
        return "暂无判定数据。"

    lines = []
    lines.append(f"时间：{latest_state.get('time', '-')}")
    lines.append(
        f"候选：{latest_state.get('candidate', '-')}    "
        f"第二名：{latest_state.get('runner_up', '-')}"
    )
    lines.append(
        f"候选绝对分贝：{latest_state.get('candidate_db', '-')}dB    "
        f"候选相对分：{latest_state.get('candidate_score', '-')}dB    "
        f"领先差：{latest_state.get('winner_gap', '-')}dB    "
        f"VAD：{latest_state.get('vad_passed', '-')}"
    )
    lines.append(f"是否计时：{latest_state.get('is_speaking', '-')}")
    lines.append(f"结论：{latest_state.get('decision', '-')}")

    db_values = latest_state.get("db_values", {})
    score_values = latest_state.get("score_values", {})
    if db_values:
        lines.append(
            "原始分贝：" + "  ".join(f"{n}={v}dB" for n, v in sorted(db_values.items()))
        )
    if score_values:
        lines.append(
            "相对分：" + "  ".join(f"{n}={v:+.1f}" for n, v in sorted(score_values.items()))
        )

    return "\n".join(lines)


def fmt_recent_events(latest_events):
    if not latest_events:
        return "暂无计时事件。"

    lines = []
    for event in latest_events[-5:]:
        lines.append(
            f"{event.get('time', '-')}  {event.get('node', '-')} "
            f"+{event.get('add_seconds', 0):.1f}s  "
            f"累计={event.get('total_seconds', 0):.1f}s  "
            f"相对分={event.get('candidate_score', '-')}  "
            f"领先={event.get('winner_gap', '-')}"
        )
    return "\n".join(lines)


def main():
    while True:
        try:
            with urllib.request.urlopen(URL, timeout=3) as resp:
                data = json.loads(resp.read())

            times = data.get("current_speaking_times", {})
            latest_state = data.get("latest_audio_state", {})
            latest_events = data.get("latest_speaking_events", [])
            table, total, avg = fmt_node_table(times)

            clear_screen()
            print("=== EffMeet 实时发言统计 ===")
            print(f"刷新间隔：{REFRESH_SECONDS}s    当前时间：{time.strftime('%H:%M:%S')}")
            print(f"总发言时长：{total:.1f}s    平均：{avg:.1f}s\n")
            print("节点   时长       占比    条形图                   状态")
            print("-" * 66)
            print(table)

            print("\n--- 最近一次音频判定 ---")
            print(fmt_recent_decision(latest_state))

            print("\n--- 最近计时事件 ---")
            print(fmt_recent_events(latest_events))

            print("\n按 Ctrl+C 退出。")

        except KeyboardInterrupt:
            print("\n已退出。")
            return
        except Exception as e:
            clear_screen()
            print("=== EffMeet 实时发言统计 ===")
            print(f"[{time.strftime('%H:%M:%S')}] 等待云端启动或接口恢复...")
            print(f"错误：{e}")
            print("\n请确认 main_brain.py 正在运行，并已显示 [READY]。")

        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
