# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import sounddevice as sd
import numpy as np
import time
import queue
import threading
import wave
import os
import random
import paho.mqtt.client as mqtt
from flask import Flask, jsonify
from faster_whisper import WhisperModel
from core.vad_engine import VADEngine

# ================= 1. 配置区 =================
SAMPLE_RATE    = 16000
CHUNK_DURATION = 0.5
BASE_DB_FLOOR  = 45.0

# MQTT
MQTT_BROKER           = "broker.emqx.io"
MQTT_PORT             = 1883
MQTT_TOPIC_CONTROL    = "esp32s3/control"      # -> 发给机器人
MQTT_TOPIC_STATUS     = "esp32s3/status"       # <- 机器人完成回复
MQTT_TOPIC_CYCLE_DONE = "effmeet/cycle/done"   # -> 一整圈干预完毕通知
SILENCE_TIMEOUT       = 120  # 每 120 秒检查一次是否需要干预

# 干预顺序：逆时针 1(上)->2(左)->3(下)->4(右)，可改数字但保持逆时针
INTERVENTION_ORDER = [1, 2, 3, 4]

# 物理座位绑定表（必须与声音设置里的名字一致）
NODE_HARDWARE_MAP = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]
# ==============================================

# ================= 2. 全局状态 =================
app = Flask(__name__)
meeting_records = []
speaking_times  = {f"node{i}": 0.0 for i in range(1, 5)}

# 机器人调度状态
_robot_busy      = False
_cycle_index     = 0
_active_targets  = []          # 本轮需要干预的目标列表（动态生成）
_cycle_lock      = threading.Lock()
_mqtt_client_ref = None

# Whisper 模型加载
print("[BOOT] 正在加载 Whisper 模型，请稍候...")
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("[BOOT] Whisper 模型加载完毕！")

audio_queues     = {f"node{i}": queue.Queue() for i in range(1, 5)}
transcribe_queue = queue.Queue()

@app.route('/api/get_meeting_data', methods=['GET'])
def get_meeting_data():
    return jsonify({
        "status": "success",
        "current_speaking_times": speaking_times,
        "latest_records": meeting_records[-10:]
    })
# ==============================================

# ================= 3. 工具函数 =================
def get_decibels(audio_bytes):
    arr = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
    return 20 * np.log10(rms + 1e-6)

def save_to_wav(audio_frames, filename):
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b''.join(audio_frames))

def find_renamed_microphones():
    target_mics = {}
    for i, dev in enumerate(sd.query_devices()):
        dev_name = dev['name'].upper()
        for expected in NODE_HARDWARE_MAP:
            if expected in dev_name and dev['max_input_channels'] > 0:
                hostapi = sd.query_hostapis(dev['hostapi'])['name']
                if "MME" in hostapi or "DirectSound" in hostapi:
                    node_key = expected.split('_')[0].lower()
                    if node_key not in target_mics:
                        target_mics[node_key] = i
    return target_mics
# ==============================================

# ================= 4. 线程 =================

def whisper_worker():
    """把音频帧转成文字"""
    if not os.path.exists("temp_audio"):
        os.makedirs("temp_audio")
    while True:
        node_name, frames, max_db = transcribe_queue.get()
        temp_file = f"temp_audio/{node_name}_{int(time.time())}.wav"
        save_to_wav(frames, temp_file)
        segments, info = whisper_model.transcribe(temp_file, beam_size=5)
        text = "".join([seg.text for seg in segments]).strip()
        if text:
            print(f"[TRANSCRIBE] {node_name}: {text}")
            meeting_records.append({
                "node": node_name,
                "time": time.strftime("%H:%M:%S"),
                "text": text,
                "decibel": round(max_db, 1)
            })
        try: os.remove(temp_file)
        except: pass


def _send_next_intervention(client):
    """发送当前序列里的干预目标给机器人"""
    target = _active_targets[_cycle_index]
    client.publish(MQTT_TOPIC_CONTROL, str(target))
    print(f"[SEND] -> esp32s3/control : '{target}'  ({_cycle_index + 1}/{len(_active_targets)})")
    sys.stdout.flush()


def _on_robot_done(client, msg_payload):
    """收到机器人 done 消息后推进序列"""
    global _robot_busy, _cycle_index
    print(f"[RECV] <- esp32s3/status : '{msg_payload}'")
    with _cycle_lock:
        _cycle_index += 1
        if _cycle_index >= len(_active_targets):
            _cycle_index = 0
            _robot_busy  = False
            client.publish(MQTT_TOPIC_CYCLE_DONE, "cycle_done")
            print("[DONE] 本轮干预结束！已发布 effmeet/cycle/done : 'cycle_done'")
        else:
            print(f"[NEXT] 收到完成，发送下一个...")
            _send_next_intervention(client)
    sys.stdout.flush()


def mqtt_monitor_worker():
    """机器人调度线程：顺序发 1->2->3->4，每次等 done 再发下一个"""
    global _robot_busy, _cycle_index, _mqtt_client_ref

    client_id = "EffMeet_Brain_" + str(random.randint(0, 9999))
    client = mqtt.Client(client_id=client_id)
    _mqtt_client_ref = client

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            c.subscribe(MQTT_TOPIC_STATUS)
            print(f"[MQTT] 已连接，订阅: {MQTT_TOPIC_STATUS}")
        else:
            print(f"[MQTT] 连接失败 rc={rc}")
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
        print("[MQTT] 机器人调度系统上线！")
    except Exception as e:
        print(f"[MQTT] 连接失败: {e}")
        return

    while True:
        time.sleep(SILENCE_TIMEOUT)
        with _cycle_lock:
            if _robot_busy:
                print("[SCHED] 机器人忙碌中，等待...")
                continue
            total = sum(speaking_times.values())
            if total > 5:
                avg_time = total / 4.0
                # 找出所有发言时间不足平均50%的节点，按发言量从少到多排序
                silent_nodes = sorted(
                    [n for n in speaking_times if speaking_times[n] < avg_time * 0.5],
                    key=lambda n: speaking_times[n]
                )
                if silent_nodes:
                    _active_targets[:] = [int(n.replace("node", "")) for n in silent_nodes]
                    _cycle_index = 0
                    _robot_busy  = True
                    print(f"[TRIGGER] 发言偏少节点（按发言量从少到多）: {silent_nodes}")
                    print(f"[TRIGGER] 本轮干预目标: {_active_targets}")
                    _send_next_intervention(client)
                else:
                    print("[SCHED] 发言分布均衡，无需干预")
        sys.stdout.flush()


def brain_worker():
    """实时监听、断句、算分贝"""
    vad_engine = VADEngine(sample_rate=SAMPLE_RATE)
    current_speaker    = None
    audio_buffer       = []
    silence_ticks      = 0
    max_db_in_sentence = 0

    print("[BRAIN] 监听与录音系统启动...")
    while True:
        if all(not q.empty() for q in audio_queues.values()):
            chunks   = {n: q.get() for n, q in audio_queues.items()}
            db_values = {n: get_decibels(chunks[n]) for n in audio_queues}
            winner_node = max(db_values, key=db_values.get)
            max_db      = db_values[winner_node]

            is_speaking = False
            if max_db > BASE_DB_FLOOR and vad_engine.is_speech(chunks[winner_node]):
                is_speaking = True
                speaking_times[winner_node] += CHUNK_DURATION

            if is_speaking:
                if current_speaker != winner_node:
                    if current_speaker and len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    current_speaker    = winner_node
                    audio_buffer       = [chunks[winner_node]]
                    max_db_in_sentence = max_db
                else:
                    audio_buffer.append(chunks[winner_node])
                    max_db_in_sentence = max(max_db_in_sentence, max_db)
                silence_ticks = 0
            else:
                silence_ticks += 1
                if silence_ticks > 3 and current_speaker is not None:
                    if len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    current_speaker = None
                    audio_buffer    = []
        else:
            time.sleep(0.01)

# ================= 5. 主程序 =================
def main():
    MICROPHONES = find_renamed_microphones()
    print(f"[BOOT] 检测到麦克风: {list(MICROPHONES.keys())}")
    if len(MICROPHONES) < 4:
        print(f"[ERROR] 只找到 {len(MICROPHONES)} 个麦克风，需要 4 个，退出。")
        print("        请在 Windows 声音设置里把 4 个麦克风分别改名为：")
        print("        NODE1_MIC / NODE2_MIC / NODE3_MIC / NODE4_MIC")
        return

    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    threading.Thread(target=whisper_worker,    daemon=True).start()
    threading.Thread(target=mqtt_monitor_worker, daemon=True).start()
    threading.Thread(target=brain_worker,      daemon=True).start()

    streams = []
    for n, i in MICROPHONES.items():
        def cb(indata, frames, time_info, status, name=n):
            audio_queues[name].put(indata.copy().tobytes())
        s = sd.InputStream(device=i, channels=1, samplerate=SAMPLE_RATE,
                           dtype='int16', blocksize=int(SAMPLE_RATE*CHUNK_DURATION), callback=cb)
        s.start()
        streams.append(s)
        print(f"[MIC] {n} 麦克风启动 (device={i})")

    print("\n[READY] 系统全部就绪！等待发言数据...")
    print(f"        干预触发：每 {SILENCE_TIMEOUT}s 检查，失衡则按序派车 1->2->3->4")
    print("        按 Ctrl+C 退出\n")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for s in streams:
            s.stop(); s.close()
        print("\n[EXIT] 退出系统")

if __name__ == "__main__":
    main()