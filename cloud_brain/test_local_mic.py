import sounddevice as sd
import numpy as np
import time
from core.vad_engine import VADEngine

def test_microphone_vad():
    print("=== 🎤 本地麦克风 & VAD 实时测试启动 ===")
    
    # 1. 加载我们昨天写好的 VAD 大脑
    vad = VADEngine(sample_rate=16000)
    
    # 2. 设定每次录音的长度 (1秒)
    duration = 1.0 
    sample_rate = 16000
    
    print("\n[准备就绪] 请对着电脑说话，按 Ctrl+C 停止测试...\n")
    
    try:
        while True:
            # 3. 直接从电脑麦克风截取 1 秒钟的声音
            recording = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
            sd.wait()  # 等待这 1 秒钟录完
            
            # 4. 把录到的声音变成 bytes，喂给 VAD
            audio_bytes = recording.tobytes()
            
            # 5. 调用大脑判断
            is_speaking = vad.is_speech(audio_bytes, threshold=0.5)
            
            if is_speaking:
                print("🟩 [有人说话] 触发加时逻辑！")
            else:
                print("🔲 [安静/噪音] 丢弃片段...")
                
    except KeyboardInterrupt:
        print("\n[测试结束] 麦克风已关闭。")

if __name__ == "__main__":
    test_microphone_vad()