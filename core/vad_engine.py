import torch
import numpy as np
import os

class VADEngine:
    def __init__(self, sample_rate=16000):
        print("[VAD] 正在初始化人声检测引擎...")
        
        # [关键修复补丁]：强行把 AI 模型的缓存目录改到没有任何中文的路径
        # 只要保证这个路径里全是英文即可，比如 C 盘或 D 盘根目录
        safe_cache_dir = "C:/torch_cache"
        os.makedirs(safe_cache_dir, exist_ok=True)
        torch.hub.set_dir(safe_cache_dir)
        
        print(f"[VAD] 模型将安全下载至纯英文路径: {safe_cache_dir} ...")
        
        # 使用 PyTorch Hub 直接调用开源的 Silero VAD 模型
        self.model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            trust_repo=True  # 允许加载外部仓库
        )
        self.sample_rate = sample_rate
        print("[VAD] ✅ 模型加载完成！")

    def is_speech(self, audio_bytes: bytes, threshold=0.5) -> bool:
        """判断传入的音频片段中是否有人在说话"""
        try:
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            audio_float32 = audio_array.astype(np.float32) / 32768.0
            tensor = torch.from_numpy(audio_float32)

            # [关键修复]：Silero VAD 要求每次只能处理 512 个采样点
            window_size = 512
            
            # 我们把 1 秒钟的声音，切成很多个 32 毫秒的小块，循环喂给模型
            for i in range(0, len(tensor) - window_size, window_size):
                chunk = tensor[i : i + window_size]
                speech_prob = self.model(chunk, self.sample_rate).item()
                
                # 只要这 1 秒钟内，有任何一个 32 毫秒的小块被判定为人声，
                # 我们就认为这整整 1 秒钟都有人在说话！
                if speech_prob > threshold:
                    print(f"  🗣️ [识别到人声] 触发时间戳: {i/16000:.2f}秒处")
                    return True 
                    
            # 如果循环完了都没发现人声，那就是纯噪音/安静
            return False
                
        except Exception as e:
            print(f"[VAD] 音频推理出错: {e}")
            return False