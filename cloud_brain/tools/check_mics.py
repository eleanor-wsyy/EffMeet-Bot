# -*- coding: utf-8 -*-
"""
麦克风连接自检脚本：确认 4 路 NODE*_MIC 是否真正连上、能否采集到声音。

用法（在 cloud_brain 目录下）：
    python -m tools.check_mics
    python -m tools.check_mics --seconds 3
    python -m tools.check_mics --verbose

它不只报"设备在不在"，还会真正打开录音流采一段音频，按分贝判断当前有没有声音信号，
从而帮你区分"设备连上了但没声音"和"根本没识别到"。

不依赖 torch / whisper，可在打包精简环境里直接跑。
"""
import argparse
import ctypes
import io
import sys

import numpy as np
import sounddevice as sd

# 统一控制台编码，避免 GBK 控制台对 emoji/中文报 UnicodeEncodeError。
if sys.platform.startswith("win"):
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

# 和 main_brain.py 保持一致：只会被识别为 NODE*_MIC 命名、且 hostapi 为
# MME 或 DirectSound 的输入设备。
NODE_HARDWARE_MAP = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]
SAMPLE_RATE = 16000


def _db(audio_int16):
    arr = np.frombuffer(audio_int16, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    return 20 * np.log10(rms + 1e-6)


def find_renamed_mics():
    """返回 {node_key: device_index}，只含名称含 NODE*_MIC 且是输入设备、hostapi 匹配的。"""
    target = {}
    for i, dev in enumerate(sd.query_devices()):
        name = dev["name"].upper()
        if dev["max_input_channels"] <= 0:
            continue
        for expected in NODE_HARDWARE_MAP:
            if expected in name:
                hostapi = sd.query_hostapis(dev["hostapi"])["name"]
                if "MME" in hostapi or "DirectSound" in hostapi:
                    key = expected.split("_")[0].lower()
                    if key not in target:
                        target[key] = i
    return target


def test_one(device_index, seconds, label):
    """打开一路录音流，采 seconds 秒，返回 (是否打开成功, 峰值分贝/平均分贝)。"""
    blocks = []
    try:
        with sd.InputStream(
            device=device_index,
            channels=1,
            samplerate=SAMPLE_RATE,
            dtype="int16",
            blocksize=int(SAMPLE_RATE * 0.5),
        ) as stream:
            # 丢弃前 0.5s（设备刚开可能有 pop/静音，避免误判）
            stream.read(int(SAMPLE_RATE * 0.5))
            for _ in range(int(seconds * 2)):
                data, _ = stream.read(int(SAMPLE_RATE * 0.5))
                blocks.append(data)
    except Exception as exc:
        return False, None, str(exc)

    if not blocks:
        return False, None, "没有采到音频块"

    all_audio = np.concatenate(blocks).astype(np.int16).tobytes()
    avg_db = _db(all_audio)
    # 平均分贝高 => 有持续声音信号；极低(<20dB) => 基本静音，可能没声音进来
    return True, avg_db, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=2.0,
                        help="每路采样秒数（默认 2，建议 >=2 更稳）")
    parser.add_argument("--verbose", action="store_true",
                        help="额外打印系统所有输入设备")
    args = parser.parse_args()

    print(f"=== 麦克风连接自检（采样 {args.seconds}s/路）===")
    sys.stdout.flush()

    if args.verbose:
        print("\n-- 系统所有输入设备 --")
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                hbit = sd.query_hostapis(dev["hostapi"])["name"]
                print(f"[{i}] {dev['name']} | in={dev['max_input_channels']} | {hbit}")
        print("")

    mics = find_renamed_mics()
    print(f"识别到 {len(mics)}/4 路 NODE*_MIC：{sorted(mics) if mics else '（无）'}")
    sys.stdout.flush()

    missing = []
    results = {}
    for key in ["node1", "node2", "node3", "node4"]:
        idx = mics.get(key)
        if idx is None:
            missing.append(key.upper())
            print(f"  ❌ {key.upper()}: 未识别到（设备未连/未命名/驱动不对）")
            continue
        ok, avg_db, err = test_one(idx, args.seconds, key.upper())
        if not ok:
            print(f"  ❌ {key.upper()}: 设备在(#{idx})但打开录音失败：{err}")
            continue
        status = "✅ 有声音" if avg_db and avg_db > 20 else "⚠️ 静音/疑似无信号"
        print(f"  {status} {key.upper()}: 设备#{idx}, 平均 {avg_db:.1f}dB")
        results[key] = avg_db

        # 给用户一个确认提示，方便现场对着说话测试
        print(f"     -> 请对 {key.upper()} 说句话并观察该行分贝是否明显变化")
        sys.stdout.flush()

    print("\n=== 结论 ===")
    ok_count = len(results)
    if missing:
        print(f"缺 {len(missing)} 路：{'、'.join(missing)}（设备没连上，或没在 Windows 里改名为 NODE*_MIC）")
    if ok_count == 4:
        print("✅ 4 路全部识别且能采集声音，可以开始实验。")
    elif ok_count > 0:
        print(f"⚠️ 识别到 {ok_count}/4；请检查缺失/静音的那几路。")
    else:
        print("❌ 没有一路可用。请插上麦克风并在 Windows 里改名为 NODE1_MIC ~ NODE4_MIC。")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
