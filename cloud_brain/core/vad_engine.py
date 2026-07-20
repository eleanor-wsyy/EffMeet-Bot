import os

import numpy as np
import torch


class VADEngine:
    def __init__(self, sample_rate=16000):
        print("[VAD] 正在初始化人声检测模型...")

        safe_cache_dir = "C:/torch_cache"
        os.makedirs(safe_cache_dir, exist_ok=True)
        torch.hub.set_dir(safe_cache_dir)

        print(f"[VAD] 模型缓存目录：{safe_cache_dir}")

        self.model, _utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        self.sample_rate = sample_rate
        print("[VAD] 人声检测模型加载完成。")

    def is_speech(self, audio_bytes: bytes, threshold=0.5) -> bool:
        try:
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            audio_float32 = audio_array.astype(np.float32) / 32768.0
            tensor = torch.from_numpy(audio_float32)

            window_size = 512
            for i in range(0, len(tensor) - window_size, window_size):
                chunk = tensor[i : i + window_size]
                speech_prob = self.model(chunk, self.sample_rate).item()

                if speech_prob > threshold:
                    print(f"[VAD] 在 {i / 16000:.2f}s 处检测到人声。")
                    return True

            return False
        except Exception as e:
            print(f"[VAD] 音频推理失败：{e}")
            return False
