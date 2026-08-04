# -*- coding: utf-8 -*-
"""
ActivityEngine: 纯判定状态机，把"音量/人声"对应到正确的麦克风。

本模块无 IO、无外部依赖（仅 numpy），可独立离线测试。它接收每个块各路麦克风的
分贝值（由调用方用 get_decibels 算出），输出当前应计时的主导说话人。

三层判定（对应 判定改进设计.md）：
  1. 每路自适应底噪：环境噪声漂移时底噪缓慢跟随；说话时停止更新。
  2. 全局双门限 VAD：高门限开口 / 低门限维持 / 静音容忍。
  3. 主导说话人归属：切换需持续证据；主导者在静音容忍(hangover)窗口内不被串音夺权。

调用方仅需：
    from core.activity_engine import ActivityEngine
    engine = ActivityEngine(nodes=["node1", ...])
    result = engine.update({"node1": db1, "node2": db2, ...})
    if result["count"]:
        speaking_times[result["dom"]] += chunk_duration
"""
from __future__ import annotations

import threading


class ActivityEngine:
    def __init__(
        self,
        nodes,
        floor_init_db=-80.0,
        speech_hi_db=10.0,
        speech_lo_db=6.0,
        floor_alpha=0.03,
        max_sil=3,
        hangover=3,
        lead_confirm=2,
    ):
        """每 0.5s 块调用一次 update。

        :param nodes: 麦克风节点名列表，如 ["node1", "node2", "node3", "node4"]
        :param floor_init_db: 底噪种子，调用方可先校准再更新。
        :param speech_hi_db: 开口高门限（相对底噪，dB）
        :param speech_lo_db: 维持低门限（相对底噪，dB）
        :param floor_alpha: 底噪自适应时间常数（0~1，越大跟得越快）
        :param max_sil: 全局 VAD 连续静音容忍块数（超过则结束说话）
        :param hangover: 主导者静音容忍块数（说话中短暂停顿不释放）
        :param lead_confirm: 新主导者接管前需要的连续块数（防单块误占）
        """
        self.nodes = list(nodes)
        self.lock = threading.Lock()

        # 每路自适应底噪
        self.floor = {node: float(floor_init_db) for node in nodes}

        self.speech_hi_db = float(speech_hi_db)
        self.speech_lo_db = float(speech_lo_db)
        self.floor_alpha = float(floor_alpha)
        self.max_sil = int(max_sil)
        self.hangover = int(hangover)
        self.lead_confirm = int(lead_confirm)

        # 全局语音活动状态
        self.vad_active = False
        self.vad_sil_ticks = 0

        # 主导者归属状态
        self.dom = None
        self.dom_hold = 0            # 主导者静音容忍计数
        self.lead_candidate = None   # 待确认的接管候选
        self.lead_count = 0          # 候选持续块数

    # -- 状态重置 ----------------------------------------------------------
    def reset(self):
        """新实验组时清零归属/活动状态，但保留自适应底噪（现场环境没变）。"""
        with self.lock:
            self.vad_active = False
            self.vad_sil_ticks = 0
            self.dom = None
            self.dom_hold = 0
            self.lead_candidate = None
            self.lead_count = 0

    # -- 对外可选底噪种子（沿用启动时一次性校准结果） -----------------------
    def set_floor(self, floor_dict):
        with self.lock:
            for node, value in floor_dict.items():
                if node in self.floor:
                    self.floor[node] = float(value)

    # -- 每块判定 -----------------------------------------------------------
    def update(self, db_values):
        """传入 {node: 分贝} 的 dict（与 nodes 对齐），返回判定结果 dict。

        返回值：
          dom        : 当前应计时的节点（None 表示不计时）
          count      : True 时调用方应给 dom 累加一个 chunk_duration
          vad_active : 整场是否处于"有持续人声"活动状态
          snr        : 每路相对底噪 {node: db}
          floor      : 每路当前底噪 {node: db}
          reason     : 便于排查的文本说明
        """
        with self.lock:
            return self._update_locked(db_values)

    def _update_locked(self, db_values):
        nodes = self.nodes
        energy = {}
        for node in nodes:
            energy[node] = float(db_values.get(node, -80.0))

        # 1) 每路自适应底噪更新（仅在未超过高门限时视为"非说话活跃"，允许跟随）
        for node in nodes:
            if energy[node] < self.floor[node] + self.speech_hi_db:
                self.floor[node] = (
                    self.floor_alpha * energy[node]
                    + (1.0 - self.floor_alpha) * self.floor[node]
                )

        # 相对底噪
        snr = {node: energy[node] - self.floor[node] for node in nodes}

        # 2) 全局双门限 VAD
        top_node = max(snr, key=snr.get)
        top_snr = snr[top_node]

        if not self.vad_active:
            if top_snr > self.speech_hi_db:
                self.vad_active = True
                self.vad_sil_ticks = 0
        else:
            if top_snr < self.speech_lo_db:
                self.vad_sil_ticks += 1
                if self.vad_sil_ticks > self.max_sil:
                    self.vad_active = False
                    self.vad_sil_ticks = 0
            else:
                self.vad_sil_ticks = 0

        # 3) 主导者归属 + 静音容忍
        dom = self.dom
        dom_hold = self.dom_hold
        lead_candidate = self.lead_candidate
        lead_count = self.lead_count

        if not self.vad_active:
            # 整场没在说话：释放主导者，但给短暂的后置容忍
            if dom is not None:
                dom_hold += 1
                if dom_hold > self.hangover:
                    dom = None
            else:
                dom_hold = 0
            lead_candidate = None
            lead_count = 0
        else:
            # 整场在说话
            if dom is None:
                # 无人主导：给最响且超开口门限的路做"持续确认"
                if top_snr > self.speech_hi_db:
                    if lead_candidate == top_node:
                        lead_count += 1
                    else:
                        lead_candidate = top_node
                        lead_count = 1
                    if lead_count >= self.lead_confirm:
                        dom = top_node
                        dom_hold = 0
                        lead_candidate = None
                        lead_count = 0
                else:
                    lead_candidate = None
                    lead_count = 0
            else:
                # 已有主导者：只有当 SAME 路连续静音超过容忍，才允许换人
                if snr[dom] < self.speech_lo_db:
                    dom_hold += 1
                    if dom_hold > self.hangover:
                        # 主导者确实停了，允许换路（重新做持续确认）
                        dom = None
                        dom_hold = 0
                        lead_candidate = None
                        lead_count = 0
                else:
                    dom_hold = 0
                    # 主导者还在说话：压制任何接管候选，防串音抢权
                    lead_candidate = None
                    lead_count = 0

        self.dom = dom
        self.dom_hold = dom_hold
        self.lead_candidate = lead_candidate
        self.lead_count = lead_count

        count = bool(self.vad_active and dom is not None)
        return {
            "dom": dom if count else None,
            "count": count,
            "vad_active": self.vad_active,
            "snr": {n: round(v, 2) for n, v in snr.items()},
            "floor": {n: round(v, 2) for n, v in self.floor.items()},
            "reason": (
                "count" if count else
                "silence" if not self.vad_active else "no-dom"
            ),
        }
