# -*- coding: utf-8 -*-
import time
import yaml
import sys
import io
# 解决 Windows 终端中文/Emoji 乱码及编码错误
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from network.mqtt_manager import MQTTManager
from logic.meeting_state import MeetingState

def run_test():
    print("=== [Test] 会议干预与MQTT下发闭环测试 ===")
    
    # 1. 加载配置
    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        
    # 为了快速看效果，我们临时在内存里把“冷却时间”改成 0 秒
    config['intervention_cooldown'] = 0 
    config['variance_threshold'] = 50

    # 2. 启动网络发报机和会议大脑
    network = MQTTManager(config, None)
    network.start()
    time.sleep(2)  # 给它 2 秒钟连上服务器
    
    meeting_state = MeetingState(config, network)

    print("\n[剧本开始] 模拟一场极其失衡的会议...")
    time.sleep(1)

    # 3. 强行给 node1 打钱（加时间），让它变成霸麦狂魔
    print("-> 强行给 node1 增加 100 秒发言时长...")
    meeting_state.add_speech_time("node1", 100.0)
    time.sleep(1)

    # 4. 让可怜的 node3 只说一句话，触发系统结算...
    print("-> 最沉默的 node3 说了 5 秒钟话，触发系统结算...")
    meeting_state.add_speech_time("node3", 5.0)
    meeting_state.check_balance()

    # 等待指令飞向云端
    time.sleep(2)
    print("\n[测试结束] 安全退出。")
    network.client.loop_stop()

if __name__ == "__main__":
    run_test()
