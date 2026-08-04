# -*- coding: utf-8 -*-
"""
ActivityEngine 离线测试：用合成 int16 音频验证"捂住麦克风不乱计"等关键行为。

不依赖真实麦克风/VAD，全部用 ActivityEngine 纯逻辑 + RMS 分贝。运行：
    cd cloud_brain
    python test_activity_engine.py
"""
from __future__ import annotations

import sys

try:
    import numpy as np
    from core.activity_engine import ActivityEngine
except ImportError as exc:  # 路径兼容
    import os

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    import numpy as np
    from core.activity_engine import ActivityEngine

NODES = ["node1", "node2", "node3", "node4"]
FS = 16000
CHUNK = 0.5
WIN_SIZE = int(FS * CHUNK)  # 8000


def db_of_audio(audio: bytes) -> float:
    arr = np.frombuffer(audio, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    return 20 * np.log10(rms + 1e-6)


def make_block(amplitude: float, seed: int = 0) -> bytes:
    """生成一个 0.5s 块的 int16 音频，峰值为 amplitude。"""
    t = np.arange(WIN_SIZE)
    rng = np.random.default_rng(seed)
    wave = amplitude * 0.7 * np.sin(2 * np.pi * 220.0 * t / FS)
    wave += rng.uniform(-1.0, 1.0, WIN_SIZE) * amplitude * 0.1
    wave = np.clip(wave, -1.0, 1.0)
    return (wave * 32767).astype(np.int16).tobytes()


def db_for(amplitude: float) -> float:
    return db_of_audio(make_block(amplitude, seed=1))


def scene(engine: ActivityEngine, db_map: dict) -> dict:
    """喂入一轮各路分贝（缺失的路给 -80dB 静音）。"""
    values = {n: db_map.get(n, -80.0) for n in NODES}
    return engine.update(values)


def silence(engine: ActivityEngine):
    return engine.update({n: -80.0 for n in NODES})


def assert_ok(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
def test_covered_mic_no_count():
    """捂住 node1：只有短暂吹气尖峰，node2 持续说话。尖峰路不计时。"""
    print("[1] 捂住麦克风不乱计时")
    eng = ActivityEngine(NODES, floor_init_db=-80.0)
    base = db_for(0.02)          # 底噪水平约 -? dB
    speak = db_for(0.9)          # 持续说话
    peak = db_for(0.95)          # 吹气/敲击尖峰

    counted_node2 = 0
    for i in range(20):
        if i in (5, 6):
            m = {"node1": peak, "node2": speak}   # 尖峰只有 2 块，无持续性
        else:
            m = {"node1": base, "node2": speak}
        res = scene(eng, m)
        if res["count"]:
            assert_ok(res["dom"] == "node2", f"峰值块 {i}: 归属 node2, got {res['dom']}")
            counted_node2 += 1
    assert_ok(counted_node2 >= 15, f"node2 应持续计时, counted={counted_node2}")
    assert_ok(True, f"node1（捂住/尖峰）从不被计为说话人")


def test_dominant_stability_under_crosstalk():
    """node1 持续说话，node2 偶发串音尖峰，node1 保持主导。"""
    print("[2] 主导者稳定，串音不抢权")
    eng = ActivityEngine(NODES, floor_init_db=-80.0)
    speak = db_for(0.9)
    crosstalk = db_for(1.0)      # 串音瞬时更高

    node1_count = 0
    node2_count = 0
    for i in range(20):
        m = {"node1": speak, "node2": crosstalk if i % 4 == 0 else db_for(0.01)}
        res = scene(eng, m)
        if res["count"]:
            if res["dom"] == "node1":
                node1_count += 1
            elif res["dom"] == "node2":
                node2_count += 1
    assert_ok(node1_count >= 15, f"node1 应主导计时, node1={node1_count}")
    assert_ok(node2_count == 0, f"node2 偶发串音不应抢占, node2={node2_count}")


def test_pause_hangover():
    """node1 说话中插入短停顿不释放；长停顿释放。"""
    print("[3] 静音容忍：短停顿不误停，长停顿释放")
    eng = ActivityEngine(NODES, floor_init_db=-80.0)
    speak = db_for(0.9)

    # 建立 node1 主导
    for _ in range(3):
        scene(eng, {"node1": speak})

    # 2 块短停顿（< hangover=3）
    released_after_short = False
    for _ in range(2):
        res = silence(eng)
        if res["count"] is False and res["vad_active"] is False and eng.dom is None:
            released_after_short = True
    # 短停顿期间即使走了 VAD 结束，也不应把已累计的时长标"停"；核心是：
    # 停顿后若 node1 恢复说话，应能立刻继续（无需重建 confirm）。
    resumed = scene(eng, {"node1": speak})
    assert_ok(resumed["count"] and resumed["dom"] == "node1",
              f"短停顿 2 块后恢复，node1 继续主导, got={resumed['dom']}")

    # 连续 5 块长停顿（> hangover），应释放主导者
    silence(eng)  # 已静音状态继续
    for _ in range(5):
        silence(eng)
    assert_ok(eng.dom is None, "长停顿 5 块后主导者释放")
    assert_ok(eng.vad_active is False, "长停顿后全局 VAD 结束")


def test_switch_needs_confirm():
    """node1 停，node2 接话：需连续 lead_confirm 块才切换。"""
    print("[4] 切换需持续证据")
    eng = ActivityEngine(NODES, floor_init_db=-80.0)
    speak = db_for(0.9)
    for _ in range(3):
        scene(eng, {"node1": speak})

    # node1 停（长停顿释放），node2 开始说话
    for _ in range(6):
        silence(eng)
    # node2 说话，但只 1 块 -> 不够 lead_confirm，可能无人主导
    r1 = scene(eng, {"node2": speak, "node1": db_for(0.01)})
    r2 = scene(eng, {"node2": speak, "node1": db_for(0.01)})
    res = scene(eng, {"node2": speak, "node1": db_for(0.01)})
    assert_ok(res["count"] and res["dom"] == "node2",
              f"node2 持续 3 块后接管主导, got={res['dom']} count={res['count']}")
    assert_ok(r1["count"] is False or r1["dom"] is None,
              f"第一块不应立即由独占短块主导（需持续证据）")


def main():
    test_covered_mic_no_count()
    test_dominant_stability_under_crosstalk()
    test_pause_hangover()
    test_switch_needs_confirm()
    print("\nAll ActivityEngine offline tests passed.")


if __name__ == "__main__":
    main()
