import os
import tempfile
from pathlib import Path

import numpy as np


class VADEngine:
    def __init__(self, sample_rate=16000):
        # torch 依赖较大，打包/纯录音场景可能不含；只在真正实例化 VAD 时才 import，
        # 这样 core.vad_engine 模块被 main_brain import 时不需要 torch 存在。
        import torch

        self._torch = torch
        print("[VAD] 正在初始化人声检测模型...")

        # 不再写死 C:/torch_cache：优先放在本程序目录下的 .cache/torch（可随程序整体拷贝），
        # 若不可写则退回系统临时目录。这样在任何电脑上都能创建缓存。
        repo_root = Path(__file__).resolve().parent.parent
        cache_candidates = [
            repo_root / ".cache" / "torch",
            Path(tempfile.gettempdir()) / "effmeet_torch_cache",
        ]
        safe_cache_dir = None
        for candidate in cache_candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                # 验证可写：写入再删除一个探针文件。
                probe = candidate / ".write_test"
                probe.write_bytes(b"ok")
                probe.unlink()
                safe_cache_dir = str(candidate)
                break
            except OSError:
                continue
        if safe_cache_dir is None:
            safe_cache_dir = str(Path(tempfile.gettempdir()) / "effmeet_torch_cache")

        self._torch.hub.set_dir(safe_cache_dir)

        print(f"[VAD] 模型缓存目录：{safe_cache_dir}")

        self.model, _utils = self._torch.hub.load(
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
            tensor = self._torch.from_numpy(audio_float32)

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
