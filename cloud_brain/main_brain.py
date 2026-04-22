import sounddevice as sd
import numpy as np
import time
import queue
import threading
import wave
import os
import json
import paho.mqtt.client as mqtt
from flask import Flask, jsonify
from faster_whisper import WhisperModel
from core.vad_engine import VADEngine

# ================= 1. 终极配置区 =================
# 音频与系统配置
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
BASE_DB_FLOOR = 45.0  

# MQTT 配置 (负责叫小车)
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC = "GAFA_Robot_Project_2026"
SILENCE_TIMEOUT = 120  # 每 120 秒检查一次有没有人被冷落

# 物理座位绑定表 (请替换成你在控制面板改好的名字)
NODE_HARDWARE_MAP = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]
# ===============================================

# ================= 2. 全局状态与接口 =================
app = Flask(__name__)
meeting_records = []  # 存转写好的文字记录
speaking_times = {f"node{i}": 0.0 for i in range(1, 5)}

# 初始化 Whisper 模型 (选 tiny 最快，跑在 CPU 上保证不报错)
print("⏳ 正在加载 Whisper 本地语音大模型，请稍候...")
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
print("✅ Whisper 模型加载完毕！")

# 音频缓冲系统 (用于把碎片段拼成一整句话)
audio_queues = {f"node{i}": queue.Queue() for i in range(1, 5)}
transcribe_queue = queue.Queue() # 等待转文字的任务队列

@app.route('/api/get_meeting_data', methods=['GET'])
def get_meeting_data():
    return jsonify({
        "status": "success",
        "current_speaking_times": speaking_times,
        "latest_records": meeting_records[-10:]
    })
# ===============================================

# ================= 3. 核心工具函数 =================
def get_decibels(audio_bytes):
    arr = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
    return 20 * np.log10(rms + 1e-6)

def save_to_wav(audio_frames, filename):
    """把字节流存成临时的 wav 文件供 Whisper 读取"""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2) # 16-bit
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
# ===============================================

# ================= 4. 各大运转线程 =================

def whisper_worker():
    """专门负责把声音文件转成文字的线程"""
    if not os.path.exists("temp_audio"):
        os.makedirs("temp_audio")
        
    while True:
        task = transcribe_queue.get() # task = (node_name, audio_frames, max_db)
        node_name, frames, max_db = task
        
        # 1. 存临时文件
        temp_file = f"temp_audio/{node_name}_{int(time.time())}.wav"
        save_to_wav(frames, temp_file)
        
        # 2. 调用大模型转写
        segments, info = whisper_model.transcribe(temp_file, beam_size=5)
        text = "".join([segment.text for segment in segments]).strip()
        
        # 3. 如果真说出了话，记入全局账本供队友调用
        if text:
            print(f"📝 [{node_name} 转写成功]: {text}")
            meeting_records.append({
                "node": node_name,
                "time": time.strftime("%H:%M:%S"),
                "text": text,
                "decibel": round(max_db, 1)
            })
            
        # 打扫战场
        try: os.remove(temp_file)
        except: pass

def mqtt_monitor_worker():
    """专门盯着时长，谁被冷落了就叫小车去哪"""
    client = mqtt.Client()
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        print(f"📡 [MQTT] 成功连接至 {MQTT_BROKER}，小车调度系统上线！")
    except Exception as e:
        print(f"⚠️ [MQTT] 连接失败，请检查网络: {e}")
        return

    while True:
        time.sleep(SILENCE_TIMEOUT)
        # 算一下谁说得最少
        if sum(speaking_times.values()) > 5: # 会议总时长大于5秒才开始评判
            most_silent_node = min(speaking_times, key=speaking_times.get)
            avg_time = sum(speaking_times.values()) / 4.0
            
            # 如果最沉默的人时长远低于平均值，触发小车！
            if speaking_times[most_silent_node] < (avg_time * 0.5):
                payload = json.dumps({
                    "action": "move",
                    "target": most_silent_node,
                    "reason": "silence_timeout"
                })
                client.publish(MQTT_TOPIC, payload)
                print(f"🚗 [调度警报] {most_silent_node} 发言太少，已派遣小车前往破冰！")

def brain_worker():
    """实时监听、断句、算分贝的主大脑"""
    vad_engine = VADEngine(sample_rate=SAMPLE_RATE)
    
    current_speaker = None
    audio_buffer = []
    silence_ticks = 0
    max_db_in_sentence = 0

    print("🧠 [云端大脑] 监听与录音系统启动...")
    while True:
        if all(not q.empty() for q in audio_queues.values()):
            chunks = {n: q.get() for n, q in audio_queues.items()}
            db_values = {n: get_decibels(chunks[n]) for n in audio_queues.keys()}
            
            winner_node = max(db_values, key=db_values.get)
            max_db = db_values[winner_node]
            
            is_speaking = False
            if max_db > BASE_DB_FLOOR and vad_engine.is_speech(chunks[winner_node]):
                is_speaking = True
                speaking_times[winner_node] += CHUNK_DURATION
                
            # --- 断句与录音逻辑（状态机） ---
            if is_speaking:
                if current_speaker != winner_node:
                    # 换人说话了！把上一个人的录音结算掉，送去转文字
                    if current_speaker and len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    # 开启新的人的录音
                    current_speaker = winner_node
                    audio_buffer = [chunks[winner_node]]
                    max_db_in_sentence = max_db
                else:
                    # 还是这个人，继续录
                    audio_buffer.append(chunks[winner_node])
                    max_db_in_sentence = max(max_db_in_sentence, max_db)
                silence_ticks = 0
            else:
                # 没人说话，增加沉默倒计时
                silence_ticks += 1
                # 如果连续安静了超过 1.5 秒 (3个tick)，就把刚才录的结算掉
                if silence_ticks > 3 and current_speaker is not None:
                    if len(audio_buffer) > 1:
                        transcribe_queue.put((current_speaker, audio_buffer, max_db_in_sentence))
                    current_speaker = None
                    audio_buffer = []
        else:
            time.sleep(0.01)

# ================= 5. 主程序启动 =================
def main():
    MICROPHONES = find_renamed_microphones()
    if len(MICROPHONES) < 4:
        print("\n❌ 物理麦克风未就绪，强制退出。")
        return

    # 启动 API 接口
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False), daemon=True).start()
    # 启动文字转写处理
    threading.Thread(target=whisper_worker, daemon=True).start()
    # 启动小车调度
    threading.Thread(target=mqtt_monitor_worker, daemon=True).start()
    # 启动大脑核心
    threading.Thread(target=brain_worker, daemon=True).start()

    # 启动硬件监听
    streams = []
    for n, i in MICROPHONES.items():
        def cb(indata, frames, time_info, status, name=n):
            audio_queues[name].put(indata.copy().tobytes())
        s = sd.InputStream(device=i, channels=1, samplerate=SAMPLE_RATE, dtype='int16', blocksize=int(SAMPLE_RATE*CHUNK_DURATION), callback=cb)
        s.start()
        streams.append(s)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        for s in streams: s.stop(); s.close()
        print("\n📊 退出系统...")

if __name__ == "__main__":
    main()