# -*- coding: utf-8 -*-
import io
import argparse
import os
import queue
import random
import sys
import threading
import time
import traceback
import wave
from collections import deque

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
from flask import Flask, jsonify
from faster_whisper import WhisperModel

from core.vad_engine import VADEngine


# 统一控制台编码，避免 Windows 双击运行时中文日志乱码。
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
if hasattr(sys.stderr, "buffer") and (sys.stderr.encoding or "").lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )


# 基础音频参数。
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
CALIBRATION_SECONDS = 3.0
ABSOLUTE_DB_FLOOR = 45.0
SPEECH_ABOVE_NOISE_DB = 10.0
WINNER_MARGIN_DB = 4.0
USE_VAD = True

# 麦克风灵敏度补偿。某一路长期偏小就填正数，长期偏大就填负数。
MIC_GAIN_OFFSETS_DB = {
    "node1": 0.0,
    "node2": 0.0,
    "node3": 0.0,
    "node4": 0.0,
}

# MQTT 通信参数：云端大脑通过这些主题和机器人端同步状态。
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC_CONTROL = "esp32s3/control"
MQTT_TOPIC_STATUS = "esp32s3/status"
MQTT_TOPIC_CYCLE_DONE = "effmeet/cycle/done"
SILENCE_TIMEOUT = 120
IMBALANCE_RATIO_THRESHOLD = 0.5
INTERVENTION_ORDER = [1, 2, 3, 4]

# Windows 录音设备需要按这些名字重命名，程序会据此绑定 4 个麦克风。
NODE_HARDWARE_MAP = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]

# Flask API 和会议实时数据。
app = Flask(__name__)
meeting_records = []
speaking_times = {f"node{i}": 0.0 for i in range(1, 5)}
speaking_events = deque(maxlen=20)
latest_audio_state = {}
state_lock = threading.Lock()

# 机器人调度状态。
_robot_busy = False
_cycle_index = 0
_active_targets = []
_last_intervention_target = None
_recent_intervention_targets = deque(maxlen=1)
_cycle_lock = threading.Lock()
_mqtt_client_ref = None
_whisper_model = None

# 每个麦克风各自维护一个音频队列，转写线程从 transcribe_queue 里取完整句子。
audio_queues = {f"node{i}": queue.Queue() for i in range(1, 5)}
transcribe_queue = queue.Queue()


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--smoke", action="store_true", help="Only check microphone discovery and exit.")
    parser.add_argument("--no-mic", action="store_true", help="Start web/MQTT without opening microphone streams.")
    parser.add_argument(
        "--loose-thresholds",
        action="store_true",
        help="Use a more permissive threshold set for live experiments.",
    )
    parser.add_argument(
        "--schedule-interval",
        type=float,
        default=SILENCE_TIMEOUT,
        help="Seconds between balance checks in MQTT scheduler.",
    )
    parser.add_argument(
        "--demo-state",
        type=str,
        default="",
        help="Seed speaking times, e.g. node1=100,node2=10,node3=0,node4=15.",
    )
    return parser.parse_args(argv)


def apply_demo_state(spec):
    if not spec:
        return

    updates = {}
    for part in spec.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"Invalid demo state item: {piece}")
        key, value = piece.split("=", 1)
        key = key.strip()
        if key not in speaking_times:
            raise ValueError(f"Unknown node in demo state: {key}")
        updates[key] = float(value.strip())

    with state_lock:
        for key, value in updates.items():
            speaking_times[key] = value

    print(f"[DEMO] 已注入发言时长: {updates}")


def reset_runtime_state():
    """Clear all in-memory session state before a fresh run."""
    global _robot_busy, _cycle_index, _active_targets, _last_intervention_target

    with state_lock:
        meeting_records.clear()
        latest_audio_state.clear()
        speaking_events.clear()
        for key in speaking_times:
            speaking_times[key] = 0.0

    with _cycle_lock:
        _robot_busy = False
        _cycle_index = 0
        _active_targets = []
        _last_intervention_target = None
        _recent_intervention_targets.clear()

    print("[RESET] 会话状态已清空，准备开始新实验。")


@app.route("/api/get_meeting_data", methods=["GET"])
def get_meeting_data():
    with state_lock:
        times = dict(speaking_times)
        total = sum(times.values())
        return jsonify(
            {
                "status": "success",
                "current_speaking_times": times,
                "total_speaking_time": total,
                "latest_records": meeting_records[-10:],
                "latest_speaking_events": list(speaking_events)[-10:],
                "latest_audio_state": dict(latest_audio_state),
            }
        )


def get_whisper_model():
    # 延迟加载 Whisper，避免导入模块时就下载/加载模型，方便排查启动问题。
    global _whisper_model
    if _whisper_model is None:
        print("[启动] 正在加载 Faster-Whisper tiny 模型，请稍等...")
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print("[启动] Faster-Whisper 模型加载完成。")
    return _whisper_model


def get_decibels(audio_bytes):
    # 将 PCM int16 音频转换成分贝，用于判断当前哪个麦克风最活跃。
    arr = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    return 20 * np.log10(rms + 1e-6)


def calibrate_noise_floor():
    print(f"[校准] 请保持现场安静 {CALIBRATION_SECONDS:.0f} 秒，正在测量各麦克风底噪...")
    samples = {node: [] for node in audio_queues}
    target_count = max(1, int(CALIBRATION_SECONDS / CHUNK_DURATION))

    while any(len(values) < target_count for values in samples.values()):
        if all(not q.empty() for q in audio_queues.values()):
            for node, q in audio_queues.items():
                if len(samples[node]) < target_count:
                    samples[node].append(get_decibels(q.get()))
        else:
            time.sleep(0.01)

    noise_floor = {
        node: float(np.median(values))
        for node, values in samples.items()
    }
    floor_text = " ".join(f"{node}={db:.1f}dB" for node, db in sorted(noise_floor.items()))
    print(f"[校准] 底噪测量完成：{floor_text}")
    return noise_floor


def save_to_wav(audio_frames, filename):
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(audio_frames))


def find_renamed_microphones():
    # 扫描系统录音设备，只接入命名为 NODE*_MIC 的 4 个麦克风。
    target_mics = {}
    for i, dev in enumerate(sd.query_devices()):
        dev_name = dev["name"].upper()
        for expected in NODE_HARDWARE_MAP:
            if expected in dev_name and dev["max_input_channels"] > 0:
                hostapi = sd.query_hostapis(dev["hostapi"])["name"]
                if "MME" in hostapi or "DirectSound" in hostapi:
                    node_key = expected.split("_")[0].lower()
                    if node_key not in target_mics:
                        target_mics[node_key] = i
    return target_mics


def whisper_worker():
    # 后台转写线程：把收集到的一段语音保存成临时 wav，再交给 Whisper 转文字。
    os.makedirs("temp_audio", exist_ok=True)
    model = get_whisper_model()

    while True:
        node_name, frames, max_db = transcribe_queue.get()
        temp_file = f"temp_audio/{node_name}_{int(time.time())}.wav"
        save_to_wav(frames, temp_file)
        try:
            segments, _info = model.transcribe(temp_file, beam_size=5)
            text = "".join(seg.text for seg in segments).strip()
            if text:
                print(f"[转写] {node_name}: {text}")
                meeting_records.append(
                    {
                        "node": node_name,
                        "time": time.strftime("%H:%M:%S"),
                        "text": text,
                        "decibel": round(float(max_db), 1),
                    }
                )
        finally:
            try:
                os.remove(temp_file)
            except OSError:
                pass


def _send_next_intervention(client):
    # 向机器人发布当前这一轮需要干预的目标座位编号。
    target = _active_targets[_cycle_index]
    client.publish(MQTT_TOPIC_CONTROL, str(target))
    print(
        f"[发送] -> {MQTT_TOPIC_CONTROL}: '{target}' "
        f"（{_cycle_index + 1}/{len(_active_targets)}）"
    )
    sys.stdout.flush()


def _on_robot_done(client, msg_payload):
    # 收到机器人 done 消息后推进调度序列；一轮结束后广播 cycle_done。
    global _robot_busy, _cycle_index
    print(f"[接收] <- {MQTT_TOPIC_STATUS}: '{msg_payload}'")

    with _cycle_lock:
        _cycle_index += 1
        if _cycle_index >= len(_active_targets):
            _cycle_index = 0
            _robot_busy = False
            client.publish(MQTT_TOPIC_CYCLE_DONE, "cycle_done")
            print(f"[完成] 本轮干预结束，已发布 {MQTT_TOPIC_CYCLE_DONE}。")
        else:
            print("[下一步] 机器人已完成，继续发送下一个干预目标。")
            _send_next_intervention(client)

    sys.stdout.flush()


def mqtt_monitor_worker(schedule_interval):
    global _robot_busy, _cycle_index, _mqtt_client_ref, _last_intervention_target

    client_id = "EffMeet_Brain_" + str(random.randint(10000, 99999))
    client = mqtt.Client(client_id=client_id)
    print(f"[MQTT] 客户端 ID: {client_id}")
    _mqtt_client_ref = client

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            c.subscribe(MQTT_TOPIC_STATUS)
            print(f"[MQTT] 已连接 Broker，并订阅机器人状态主题：{MQTT_TOPIC_STATUS}")
        else:
            print(f"[MQTT] 连接异常，返回码：{rc}")
        sys.stdout.flush()

    def on_message(c, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if payload.startswith("done"):
            _on_robot_done(c, payload)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print("[MQTT] 机器人调度线程已上线。")
    except Exception as e:
        print(f"[MQTT] 连接失败：{e}")
        return

    while True:
        time.sleep(schedule_interval)
        with _cycle_lock:
            if _robot_busy:
                print("[调度] 机器人正在执行任务，等待下一轮检查。")
                continue

            with state_lock:
                total = sum(speaking_times.values())
                sorted_nodes = sorted(speaking_times.items(), key=lambda x: x[1])

            if total <= 5:
                continue

            avg_time = total / 4.0
            threshold = avg_time * IMBALANCE_RATIO_THRESHOLD

            candidate_node, candidate_time = sorted_nodes[0]
            is_avoided = False
            last_target = _recent_intervention_targets[-1] if _recent_intervention_targets else None
            if candidate_node == last_target and len(sorted_nodes) > 1:
                candidate_node, candidate_time = sorted_nodes[1]
                is_avoided = True

            if candidate_time < threshold:
                target_num = int(candidate_node.replace("node", ""))
                _active_targets[:] = [target_num]
                _cycle_index = 0
                _robot_busy = True
                avoid_msg = (
                    f" 已跳过上一轮刚干预过的 {_last_intervention_target}；"
                    if is_avoided
                    else ""
                )
                _last_intervention_target = candidate_node
                _recent_intervention_targets.append(candidate_node)
                print(
                    f"[触发]{avoid_msg}{candidate_node} 发言 "
                    f"{candidate_time:.1f}s，低于阈值 {threshold:.1f}s"
                )
                print(f"[触发] 本轮干预目标：{_active_targets}")
                _send_next_intervention(client)
            else:
                print(
                    f"[调度] 发言分布暂时均衡。候选节点 {candidate_node}: "
                    f"{candidate_time:.1f}s >= {threshold:.1f}s"
                )

        sys.stdout.flush()

def brain_worker():
    # 后台音频分析线程：持续读取 4 路麦克风，统计发言时长并切分可转写片段。
    vad_engine = None
    if USE_VAD:
        try:
            vad_engine = VADEngine(sample_rate=SAMPLE_RATE)
        except Exception as e:
            print(f"[VAD] 初始化失败，将临时退回到纯分贝判断：{e}")
    current_speaker = None
    audio_buffer = []
    silence_ticks = 0
    max_db_in_sentence = 0
    last_debug_print = 0
    noise_floor = calibrate_noise_floor()

    print("[大脑] 音频监听与分析线程已启动。")
    while True:
        if all(not q.empty() for q in audio_queues.values()):
            chunks = {n: q.get() for n, q in audio_queues.items()}
            db_values = {n: get_decibels(chunks[n]) for n in audio_queues}
            score_values = {
                n: db_values[n] - noise_floor[n] + MIC_GAIN_OFFSETS_DB.get(n, 0.0)
                for n in audio_queues
            }
            ranked_nodes = sorted(score_values, key=score_values.get, reverse=True)
            winner_node = ranked_nodes[0]
            runner_up_node = ranked_nodes[1] if len(ranked_nodes) > 1 else winner_node
            max_db = db_values[winner_node]
            max_score = score_values[winner_node]
            runner_up_score = score_values[runner_up_node]
            winner_gap = max_score - runner_up_score

            # 每隔 2 秒打印一次分贝，便于现场校准麦克风阈值。
            now = time.time()
            if now - last_debug_print >= 2:
                db_text = " ".join(f"{n}={db_values[n]:.1f}dB" for n in sorted(db_values))
                score_text = " ".join(f"{n}={score_values[n]:+.1f}" for n in sorted(score_values))
                print(
                    f"[分贝] {db_text} | 相对分={score_text} | "
                    f"候选={winner_node} 绝对={max_db:.1f}dB 相对={max_score:.1f}dB "
                    f"领先={winner_gap:.1f}dB"
                )
                last_debug_print = now

            # VAD 可用时叠加人声判断；VAD 初始化失败时退回到纯分贝判断。
            vad_passed = True if vad_engine is None else vad_engine.is_speech(chunks[winner_node])
            is_speaking = (
                max_db > ABSOLUTE_DB_FLOOR
                and max_score > SPEECH_ABOVE_NOISE_DB
                and winner_gap >= WINNER_MARGIN_DB
                and vad_passed
            )

            if not is_speaking:
                if max_db <= ABSOLUTE_DB_FLOOR:
                    decision = f"未计时：绝对分贝 {max_db:.1f}dB 低于 {ABSOLUTE_DB_FLOOR:.1f}dB"
                elif max_score <= SPEECH_ABOVE_NOISE_DB:
                    decision = f"未计时：相对底噪 {max_score:.1f}dB 低于 {SPEECH_ABOVE_NOISE_DB:.1f}dB"
                elif winner_gap < WINNER_MARGIN_DB:
                    decision = f"未计时：领先差 {winner_gap:.1f}dB 小于 {WINNER_MARGIN_DB:.1f}dB，疑似串音"
                elif not vad_passed:
                    decision = "未计时：VAD 未判定为人声"
                else:
                    decision = "未计时"
            else:
                decision = f"计时：{winner_node} +{CHUNK_DURATION:.1f}s"

            with state_lock:
                latest_audio_state.clear()
                latest_audio_state.update(
                    {
                        "time": time.strftime("%H:%M:%S"),
                        "candidate": winner_node,
                        "runner_up": runner_up_node,
                        "candidate_db": round(float(max_db), 1),
                        "candidate_score": round(float(max_score), 1),
                        "runner_up_score": round(float(runner_up_score), 1),
                        "winner_gap": round(float(winner_gap), 1),
                        "vad_passed": bool(vad_passed),
                        "is_speaking": bool(is_speaking),
                        "decision": decision,
                        "db_values": {n: round(float(v), 1) for n, v in db_values.items()},
                        "score_values": {n: round(float(v), 1) for n, v in score_values.items()},
                        "noise_floor": {n: round(float(v), 1) for n, v in noise_floor.items()},
                    }
                )

            if is_speaking:
                with state_lock:
                    speaking_times[winner_node] += CHUNK_DURATION
                    speaking_events.append(
                        {
                            "time": time.strftime("%H:%M:%S"),
                            "node": winner_node,
                            "add_seconds": CHUNK_DURATION,
                            "total_seconds": round(float(speaking_times[winner_node]), 1),
                            "candidate_db": round(float(max_db), 1),
                            "candidate_score": round(float(max_score), 1),
                            "winner_gap": round(float(winner_gap), 1),
                        }
                    )

                # 说话人变化时，把上一位说话人的缓存片段送去转写。
                if current_speaker != winner_node:
                    if current_speaker and len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    current_speaker = winner_node
                    audio_buffer = [chunks[winner_node]]
                    max_db_in_sentence = max_db
                else:
                    audio_buffer.append(chunks[winner_node])
                    max_db_in_sentence = max(max_db_in_sentence, max_db)
                silence_ticks = 0
            else:
                silence_ticks += 1
                if silence_ticks > 3 and current_speaker is not None:
                    # 连续静音后认为一句话结束，将缓存音频交给转写线程。
                    if len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    current_speaker = None
                    audio_buffer = []
        else:
            time.sleep(0.01)


def main():
    # 程序入口：检查麦克风、启动 Web API、转写、MQTT 调度和音频分析线程。
    args = parse_args(sys.argv[1:])
    if args.schedule_interval > 0:
        global SILENCE_TIMEOUT
        SILENCE_TIMEOUT = args.schedule_interval

    if args.loose_thresholds:
        global ABSOLUTE_DB_FLOOR, SPEECH_ABOVE_NOISE_DB, WINNER_MARGIN_DB
        ABSOLUTE_DB_FLOOR = 38.0
        SPEECH_ABOVE_NOISE_DB = 8.0
        WINNER_MARGIN_DB = 3.0
        print(
            f"[THRESHOLD] loose mode enabled: "
            f"ABS={ABSOLUTE_DB_FLOOR:.1f}dB, SCORE={SPEECH_ABOVE_NOISE_DB:.1f}dB, GAP={WINNER_MARGIN_DB:.1f}dB"
        )

    reset_runtime_state()
    microphones = find_renamed_microphones()
    print(f"[启动] 已检测到麦克风节点：{list(microphones.keys())}")

    if len(microphones) < 4 and not args.no_mic:
        print(f"[错误] 只找到 {len(microphones)} 个麦克风，需要 4 个。")
        print("请在 Windows 录音设备中把 4 个麦克风分别重命名为：")
        print("NODE1_MIC / NODE2_MIC / NODE3_MIC / NODE4_MIC")
        return

    if args.smoke:
        print("[SMOKE] 麦克风检查通过，跳过 Web / MQTT / 音频线程启动。")
        return

    if args.no_mic:
        print("[NO-MIC] 跳过麦克风流启动，保留 Web / MQTT 调度。")
        if args.demo_state:
            try:
                apply_demo_state(args.demo_state)
            except Exception as e:
                print(f"[DEMO] 发言时长注入失败：{e}")
                return

    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    threading.Thread(target=whisper_worker, daemon=True).start()
    threading.Thread(target=mqtt_monitor_worker, args=(SILENCE_TIMEOUT,), daemon=True).start()
    if not args.no_mic:
        threading.Thread(target=brain_worker, daemon=True).start()

    streams = []
    if not args.no_mic:
        for node_name, device_index in microphones.items():
            def cb(indata, frames, time_info, status, name=node_name):
                # sounddevice 回调里只做轻量入队，复杂处理交给后台线程。
                audio_queues[name].put(indata.copy().tobytes())

            stream = sd.InputStream(
                device=device_index,
                channels=1,
                samplerate=SAMPLE_RATE,
                dtype="int16",
                blocksize=int(SAMPLE_RATE * CHUNK_DURATION),
                callback=cb,
            )
            stream.start()
            streams.append(stream)
            print(f"[麦克风] {node_name} 已启动，设备编号={device_index}")

    print("\n[READY] 系统全部就绪，等待发言数据...")
    print(f"        调度器每 {SILENCE_TIMEOUT}s 检查一次发言分布。")
    print(
        f"        当前阈值：ABS={ABSOLUTE_DB_FLOOR:.1f}dB, "
        f"SCORE={SPEECH_ABOVE_NOISE_DB:.1f}dB, GAP={WINNER_MARGIN_DB:.1f}dB"
    )
    print("        按 Ctrl+C 退出。\n")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for stream in streams:
            stream.stop()
            stream.close()
        print("\n[退出] 系统已停止。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\n按回车退出...")
