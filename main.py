import time
import yaml
from network.mqtt_manager import MQTTManager
from logic.meeting_state import MeetingState
from utils.audio_buffer import AudioStreamManager
from utils.report_gen import ReportGenerator  # [新增] 引入报告生成器

def load_config(config_path="config.yaml"):
    """加载 YAML 配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def main():
    print("=== EffMeet-Bot 云端中枢启动 ===")
    
    # 1. 加载配置
    config = load_config()
    
    # 2. 初始化核心组件 (依赖顺序非常关键)
    # 因为它们互相需要，我们先创建一个空的网络管理器 (暂时传 None)
    network = MQTTManager(config, None) 
    
    # 初始化大脑，把网络发报机交给它 (方便大脑在方差过大时下发移动指令)
    meeting_state = MeetingState(config, network)
    
    # 初始化音频拼接器，把大脑交给它 (方便拼接器把 VAD 识别到的人声时长记上去)
    audio_stream = AudioStreamManager(meeting_state=meeting_state)
    
    # 最后把拼接器挂载回网络管理器
    network.audio_stream = audio_stream
    
    # 3. 启动网络监听
    network.start()
    
    # 4. 模拟主程序的运行循环 (保持服务器不退出)
    try:
        while True:
            # 主循环挂起，真正的业务逻辑都在 MQTT 回调和音频流水线里跑
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\n[系统关闭] 检测到中断信号...")
        
        # ---- [核心新增] 在关闭前，让系统生成最终报告 ----
        try:
            report_gen = ReportGenerator(meeting_state)
            report_gen.generate_excel_report()
        except Exception as e:
            print(f"[警告] 报告生成失败: {e}")
        # ---------------------------------------------
        
        print("[系统关闭] 正在停止网络服务，安全退出。")
        network.client.loop_stop()

if __name__ == "__main__":
    main()