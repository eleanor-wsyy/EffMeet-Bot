import paho.mqtt.client as mqtt
import json

class MQTTManager:
    def __init__(self, config, audio_stream=None):
        self.config = config
        self.audio_stream = audio_stream
        
        self.topic_control   = config['mqtt']['topic_control']
        self.topic_status    = config['mqtt']['topic_status']
        self.topic_cycle_done = config['mqtt'].get('topic_cycle_done', 'effmeet/cycle/done')
        
        client_id = config['mqtt'].get('client_id', 'EffMeet_Cloud_Core')
        self.client = mqtt.Client(client_id=client_id)
        
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self):
        print("[MQTT] 正在初始化通信链路...")
        try:
            self.client.connect(self.config['mqtt']['broker'], self.config['mqtt']['port'], 60)
            self.client.loop_start()
        except Exception as e:
            print(f"[MQTT] 连接失败: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"✅ [MQTT] 成功连接服务器: {self.config['mqtt']['broker']}")
            # 订阅音频流和机器人状态
            client.subscribe(self.topic_status)
            print(f"[MQTT] 已订阅机器人状态主题: {self.topic_status}")
        else:
            print(f"❌ [MQTT] 连接异常，错误码: {rc}")

    def _on_message(self, client, userdata, msg):
        """处理来自端侧的消息（机器人完成回复 done|dir=X）"""
        payload = msg.payload.decode('utf-8', errors='ignore').strip()
        print(f"[MQTT] 收到消息 [{msg.topic}]: {payload}")
        
        # done 回复可供上层逻辑处理
        if payload.startswith('done') and self.audio_stream:
            # 不同模块可根据需要进一步处理
            pass

    def send_command(self, action, target_node):
        """
        下发控制指令给小车
        target_node: 目标节点编号，如 1 / 2 / 3 / 4
        直接发纯数字字符串，ESP32 mqttCallback 只认这个格式
        """
        # 从 node_key 提取编号，如 "node1" → "1"
        if isinstance(target_node, str) and target_node.startswith('node'):
            num = target_node.replace('node', '')
        else:
            num = str(target_node)
        
        self.client.publish(self.topic_control, num)
        print(f"🚗 [MQTT指令下发] {num} → {self.topic_control}")