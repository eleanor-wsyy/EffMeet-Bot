import numpy as np
import time

class MeetingState:
    def __init__(self, config, network):
        self.config = config
        self.network = network
        
        # 初始化 4 个席位的发言时长（可以根据需要扩充到 8 个）
        self.users = {
            "node1": 0.0,
            "node2": 0.0,
            "node3": 0.0,
            "node4": 0.0
        }
        
        self.last_intervention_time = 0
        self.intervention_cooldown = config.get('intervention_cooldown', 60) # 冷却时间
        self.variance_threshold = config.get('variance_threshold', 100)      # 方差阈值

    def add_speech_time(self, device_id, duration):
        """记录发言时长并检查是否需要干预"""
        if device_id in self.users:
            self.users[device_id] += duration
            print(f"📊 [时长统计] {device_id} 总发言: {self.users[device_id]:.1f} 秒")
            self.check_balance()

    def check_balance(self):
        """计算方差，判断话语权是否失衡"""
        times = list(self.users.values())
        if sum(times) < 10:  # 会议刚开始（总时长太短）不触发干预
            return
            
        variance = np.var(times)
        
        # 如果方差超过阈值，且不在冷却期内
        if variance > self.variance_threshold:
            current_time = time.time()
            if current_time - self.last_intervention_time > self.intervention_cooldown:
                self._trigger_intervention(times)
                self.last_intervention_time = current_time

    def _trigger_intervention(self, times):
        """触发弱引导动作：找到那个最沉默的人"""
        # 找到发言时间最少的人的索引
        silent_index = np.argmin(times)
        silent_user = list(self.users.keys())[silent_index]
        
        print(f"\n⚠️ [触发干预] 话语权严重失衡！准备引导小车走向: {silent_user}")
        
        # [核心优化]：直接下发节点 ID，让小车通过 NFC 标签识别目标
        if self.network:
            self.network.send_command(action="move", target_node=silent_user)