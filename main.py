import time
import yaml
from network.mqtt_manager import MQTTManager

def load_config(config_path="config.yaml"):
    """加载 YAML 配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def main():
    print("=== EffMeet-Bot 云端中枢启动 ===")
    
    # 1. 加载配置
    config = load_config()
    
    # 2. 启动 MQTT 通信网络
    network = MQTTManager(config)
    network.start()
    
    # 3. 模拟主程序的运行循环 (保持服务器不退出)
    try:
        while True:
            # 这里是未来的主循环，目前先让它每隔 10 秒给小车发个心跳测试
            time.sleep(10)
            print("\n[系统运行中] 正在监听端侧数据...")
            
            # 模拟：如果算出方差过大，给小车下发引导指令 (仅作测试演示)
            # network.send_command(action="move", target_color="blue")
            
    except KeyboardInterrupt:
        print("\n[系统关闭] 检测到中断信号，正在退出...")
        network.client.loop_stop()

if __name__ == "__main__":
    main()