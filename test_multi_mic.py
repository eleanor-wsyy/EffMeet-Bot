import sounddevice as sd
import numpy as np
import time
import queue
import threading
from core.vad_engine import VADEngine

# ================= 配置区 =================
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
BASE_NOISE_FLOOR = 200  
# 物理搓麦克风的音量阈值（搓海绵的声音极大，通常在 1000~5000 之间）
CALIBRATION_THRESHOLD = 1500 
# ==========================================

def interactive_calibration():
    """硬核向导：通过物理搓动声音，让麦克风自己报名！"""
    print("\n🔍 正在搜索系统的 AB13X 麦克风...")
    raw_mics = []
    
    # 1. 盲抓 4 个 AB13X 麦克风的底层 ID
    for i, dev in enumerate(sd.query_devices()):
        if "AB13X" in dev['name'] and dev['max_input_channels'] > 0:
            hostapi_name = sd.query_hostapis(dev['hostapi'])['name']
            if "DirectSound" in hostapi_name:
                raw_mics.append(i)
                
    if len(raw_mics) < 4:
        print(f"❌ 警告：只找到了 {len(raw_mics)} 个麦克风！请确保 4 个麦克风都插好了。")
        return None

    print(f"✅ 成功找到 4 个麦克风！进入【物理座位绑定向导】...")
    
    # 2. 开启 4 个临时监听流用于感知敲击
    calib_queues = {mid: queue.Queue() for mid in raw_mics}
    streams = []
    
    for mid in raw_mics:
        def cb(indata, frames, time_info, status, m=mid):
            calib_queues[m].put(indata.copy().tobytes())
        s = sd.InputStream(device=mid, channels=1, samplerate=SAMPLE_RATE, dtype='int16', callback=cb)
        s.start()
        streams.append(s)

    mapping = {}
    nodes = ["node1", "node2", "node3", "node4"]
    
    # 3. 挨个要求用户去摸麦克风
    for node in nodes:
        print(f"\n👉 请用手指【用力搓一下】放在 {node} 座位上的麦克风海绵！")
        found = False
        while not found:
            for mid in raw_mics:
                if mid in mapping.values(): 
                    continue # 这个麦克风已经认主了，跳过
                
                if not calib_queues[mid].empty():
                    data = calib_queues[mid].get()
                    arr = np.frombuffer(data, dtype=np.int16)
                    rms = np.sqrt(np.mean(arr.astype(np.float32)**2))
                    
                    # 如果音量巨大，说明被人搓了！
                    if rms > CALIBRATION_THRESHOLD:
                        print(f"  🔒 标定成功！{node} 已死死绑定到底层麦克风 (音量: {int(rms)})")
                        mapping[node] = mid
                        found = True
                        
                        # 清空所有队列，防止一次搓动触发两次绑定
                        for q in calib_queues.values():
                            while not q.empty(): q.get()
                        
                        time.sleep(1) # 缓冲1秒，等你把手拿开
                        break
            time.sleep(0.01)

    # 4. 标定结束，关掉临时流
    for s in streams:
        s.stop()
        s.close()
        
    print("\n🎉 物理标定全部完成！这 4 个席位现在绝对不会弄错了！")
    return mapping

# ==========================================
# 1. 启动安装向导
MICROPHONES = interactive_calibration()

if not MICROPHONES:
    print("系统强制拦截启动！")
    exit()

# 2. 正式启动大脑
print("\n=== 🚀 启动四路并发收音与【赢家通吃】AI 大脑 ===")
vad_engine = VADEngine(sample_rate=SAMPLE_RATE)

audio_queues = {node: queue.Queue() for node in MICROPHONES.keys()}
speaking_times = {node: 0.0 for node in MICROPHONES.keys()}

def make_callback(node_name):
    def callback(indata, frames, time_info, status):
        audio_queues[node_name].put(indata.copy().tobytes())
    return callback

def brain_worker():
    print("🧠 [云端大脑] 监听中，请开始你们的会议...\n")
    while True:
        all_ready = all(not q.empty() for q in audio_queues.values())
        
        if all_ready:
            chunks = {}
            rms_values = {}
            
            for node_name, q in audio_queues.items():
                audio_bytes = q.get()
                chunks[node_name] = audio_bytes
                
                audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                rms_volume = np.sqrt(np.mean(audio_array.astype(np.float32)**2))
                rms_values[node_name] = rms_volume
            
            winner_node = max(rms_values, key=rms_values.get)
            max_rms = rms_values[winner_node]
            
            if max_rms > BASE_NOISE_FLOOR:
                if vad_engine.is_speech(chunks[winner_node], threshold=0.5):
                    speaking_times[winner_node] += CHUNK_DURATION
                    print(f"👑 [精准识别] {winner_node} 发言! (音量:{int(max_rms)}) | 累计:{speaking_times[winner_node]:.1f}秒")
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
        
        print("\n📊 === 最终会议发言时长精准统计 === 📊")
        for node, duration in speaking_times.items():
            print(f" - {node} 席位: {duration:.1f} 秒")

if __name__ == "__main__":
    run_multi_test()