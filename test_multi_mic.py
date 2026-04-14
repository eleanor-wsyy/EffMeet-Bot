import sounddevice as sd
import numpy as np
import time
import queue
import threading
from core.vad_engine import VADEngine

# ================= 配置区 =================
MICROPHONES = {
    "node1": 14,  # 对应: 麦克风 (AB13X USB Audio)
    "node2": 15,  # 对应: 麦克风 (3- AB13X USB Audio)
    "node3": 16,  # 对应: 麦克风 (5- AB13X USB Audio)
    "node4": 13   # 对应: 麦克风 (6- AB13X USB Audio)
}
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
# ==========================================

print("=== 🚀 启动四路并发收音与 VAD 大脑 ===")
print("正在加载 AI 模型...")
vad_engine = VADEngine(sample_rate=SAMPLE_RATE)

# 建立 4 条“传送带”（队列）和 4 个人的时长账本
audio_queues = {node: queue.Queue() for node in MICROPHONES.keys()}
speaking_times = {node: 0.0 for node in MICROPHONES.keys()}

# 这是一个制造“收音小弟”的工厂函数
def make_callback(node_name):
    def callback(indata, frames, time_info, status):
        # 只要麦克风一有声音，小弟就把它扔进传送带，绝不堵塞！
        if status:
            print(f"[{node_name}] 硬件状态警告: {status}")
        audio_queues[node_name].put(indata.copy().tobytes())
    return callback

# 【核心大脑】专门负责从传送带拿声音，丢给 VAD 算时间
def brain_worker():
    print("\n🧠 [云端大脑] 已上线，正在同时监听 4 个频道...\n")
    while True:
        for node_name, q in audio_queues.items():
            if not q.empty():
                audio_bytes = q.get()
                # 拿到了声音，立刻判断是不是人话
                if vad_engine.is_speech(audio_bytes, threshold=0.5):
                    speaking_times[node_name] += CHUNK_DURATION
                    print(f"🟩 [{node_name}] 正在发言! 累计: {speaking_times[node_name]:.1f} 秒")
        # 大脑稍微喘口气，防止 CPU 跑到 100%
        time.sleep(0.01)

def run_multi_test():
    streams = []
    try:
        # 1. 启动大脑线程
        brain_thread = threading.Thread(target=brain_worker, daemon=True)
        brain_thread.start()

        # 2. 召唤 4 个收音小弟，绑定对应的麦克风 ID
        for node_name, device_id in MICROPHONES.items():
            stream = sd.InputStream(
                device=device_id,
                channels=1,
                samplerate=SAMPLE_RATE,
                dtype='int16',
                blocksize=int(SAMPLE_RATE * CHUNK_DURATION),
                callback=make_callback(node_name)
            )
            stream.start()
            streams.append(stream)
            print(f"🎤 [{node_name}] 麦克风 (ID:{device_id}) 监听已开启！")

        # 3. 让主程序永远活着，直到你按 Ctrl+C
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[测试结束] 正在关闭所有麦克风...")
        for stream in streams:
            stream.stop()
            stream.close()
        
        print("\n📊 === 最终会议发言时长统计 ===")
        for node, duration in speaking_times.items():
            print(f"{node}: {duration:.1f} 秒")

if __name__ == "__main__":
    run_multi_test()