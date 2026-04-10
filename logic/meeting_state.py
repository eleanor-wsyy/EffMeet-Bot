import time
import numpy as np

class MeetingState:
    def __init__(self, config, network_manager):
        print("[大脑] 会议状态追踪器已启动...")
        # 初始化 4 个人的发言时长字典 (单位：秒)
        self.users = {"node1": 0.0, "node2": 0.0, "node3": 0.0, "node4": 0.0}
        
        # 从 config.yaml 中读取阈值
        self.variance_threshold = config['logic']['variance_threshold']
        self.cooldown = config['logic']['cooldown_seconds']
        
        self.network = network_manager
        self.last_trigger_time = 0  # 记录上次小车出动的时间

    def add_speech_time(self, device_id: str, duration_sec: float):
        """增加某个设备的发言时长"""
        if device_id in self.users:
            self.users[device_id] += duration_sec
            print(f"📊 [时长统计] {device_id} 总发言: {self.users[device_id]:.1f} 秒")
            
            # 每次有人说话，就检查一下目前的发言是否均衡
            self.check_variance()

    def check_variance(self):
        """计算方差，决定是否出动小车"""
        times = list(self.users.values())
        
        # 核心算法：用 numpy 计算方差
        variance = np.var(times)
        
        if variance > self.variance_threshold:
            now = time.time()
            # 检查是否在冷却期内 (防止小车像无头苍蝇一样疯狂乱转)
            if now - self.last_trigger_time > self.cooldown:
                self._trigger_intervention(times)
                self.last_trigger_time = now
            else:
                # 冷却中，保持静默
                pass

    def _trigger_intervention(self, times):
        """触发弱引导动作：找到那个最沉默的人"""
        # 找到发言时长最短（最沉默）的那个设备的 ID
        silent_index = np.argmin(times)
        silent_user = list(self.users.keys())[silent_index]
        
        print(f"\n⚠️ [触发干预] 话语权严重失衡！准备引导小车走向最沉默的: {silent_user}")
        
        # 建立座位颜色映射 (假设组装时约定好的颜色)
        color_map = {
            "node1": "red", 
            "node2": "blue", 
            "node3": "green", 
            "node4": "yellow"
        }
        target_color = color_map.get(silent_user, "red")
        
        # 调用网络层，给小车下达移动指令！
        self.network.send_command(action="move", target_color=target_color)