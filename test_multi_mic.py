import sounddevice as sd
import numpy as np
import time
import queue
import threading
from core.vad_engine import VADEngine

# ================= 终极配置区 =================
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
# 基础分贝门限：过滤掉没说话时的环境白噪音（通常环境底噪在 30~50dB 左右）
BASE_DB_FLOOR = 45.0  
# ==========================================

def get_decibels(audio_bytes):
    """将声音字节流转换为人类直觉的 分贝(dB) 值"""
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
    # 计算均方根能量 (RMS)
    rms = np.sqrt(np.mean(audio_array.astype(np.float32)**2))
    # 转换为分贝 (加 1e-6 防止 log(0) 报错)
    db = 20 * np.log10(rms + 1e-6)
    return db

def find_renamed_microphones():
    """精确寻址：直接去抓取我们在 Windows 里改好的专属名字"""
    print("🔍 正在扫描系统底层的专属麦克风...")
    target_mics = {}
    expected_names = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]
    
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        # 匹配我们刚才在系统里改的名字（忽略大小写）
        dev_name_upper = dev['name'].upper()
        for expected in expected_names:
            if expected in dev_name_upper and dev['max_input_channels'] > 0:
                hostapi_name = sd.query_hostapis(dev['hostapi'])['name']
                # 优先选用兼容性最强的 MME 或 DirectSound
                if "MME" in hostapi_name or "DirectSound" in hostapi_name:
                    node_key = expected.split('_')[0].lower() # 提取 node1, node2...
                    if node_key not in target_mics:
                        target_mics[node_key] = i
                        print(f"  🔒 物理锁定成功: [{node_key}] -> {dev['name']} (底层ID: {i})")
                        
    return target_mics

# 1. 抓取麦克风
MICROPHONES = find_renamed_microphones()

if len(MICROPHONES) < 4:
    print("\n❌ 警告：未找齐 4 个专属麦克风！")
    print("请确认是否已在 Windows 声音控制面板中将它们重命名为 NODE1_MIC 到 NODE4_MIC。")
    exit()

print("\n=== 🚀 启动四路并发收音与【分贝争夺战】AI 大脑 ===")
vad_engine = VADEngine(sample_rate=SAMPLE_RATE)

audio_queues = {node: queue.Queue() for node in MICROPHONES.keys()}
speaking_times = {node: 0.0 for node in MICROPHONES.keys()}

def make_callback(node_name):
    def callback(indata, frames, time_info, status):
        audio_queues[node_name].put(indata.copy().tobytes())
    return callback

def brain_worker():
    print("🧠 [云端大脑] 监听中，将以【最高分贝】作为唯一发言依据...\n")
    while True:
        # 等待 4 个麦克风齐步走
        if all(not q.empty() for q in audio_queues.values()):
            chunks = {}
            db_values = {}
            
            # 取出数据并计算分贝
            for node_name, q in audio_queues.items():
                audio_bytes = q.get()
                chunks[node_name] = audio_bytes
                db_values[node_name] = get_decibels(audio_bytes)
            
            # 【分贝争夺战核心】：找出分贝最高的那个人！
            winner_node = max(db_values, key=db_values.get)
            max_db = db_values[winner_node]
            
            # 1. 最高分贝必须大于环境底噪（说明真有人在说话，不是空气声）
            if max_db > BASE_DB_FLOOR:
                # 2. 最高分贝的那个音频，送给 AI 去验证是不是人话（排除敲桌子、咳嗽）
                if vad_engine.is_speech(chunks[winner_node], threshold=0.5):
                    speaking_times[winner_node] += CHUNK_DURATION
                    print(f"🎤 [有效发言] {winner_node} 胜出! (分贝:{max_db:.1f}dB) | 累计时长:{speaking_times[winner_node]:.1f}秒")
        else:
            time.sleep(0.01)

def run_multi_test():
    streams = []
    try:
        brain_thread = threading.Thread(target=brain_worker, daemon=True)
        brain_thread.start()

        for node_name, device_id in MICROPHONES.items():
            stream = sd.InputStream(
                device=device_id, channels=1, samplerate=SAMPLE_RATE,
                dtype='int16', blocksize=int(SAMPLE_RATE * CHUNK_DURATION),
                callback=make_callback(node_name)
            )
            stream.start()
            streams.append(stream)

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[测试结束] 正在关闭音频流...")
        for stream in streams:
            stream.stop()
            stream.close()
        
        print("\n📊 === 最终会议发言时长精确统计 === 📊")
        for node, duration in speaking_times.items():
            print(f" - {node} 席位: {duration:.1f} 秒")

if __name__ == "__main__":
    run_multi_test()