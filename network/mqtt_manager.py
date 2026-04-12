import paho.mqtt.client as mqtt
import json

class MQTTManager:
    def __init__(self, config, audio_stream=None):
        self.config = config
        self.audio_stream = audio_stream
        
        # [修复] 完美适配你真实的 config.yaml 键名
        self.topic_audio = config['mqtt']['topic_sub']
        self.topic_pub = config['mqtt']['topic_control']
        
        # 顺便把你配置里的专属身份证 client_id 也用上
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
            self.client.subscribe(self.topic_audio)
            print(f"[MQTT] 已订阅监听主题: {self.topic_audio}")
        else:
            print(f"❌ [MQTT] 连接异常，错误码: {rc}")

    def _on_message(self, client, userdata, msg):
        """处理来自端侧的消息"""
        topic = msg.topic
        payload = msg.payload
        
        try:
            # 提取设备 ID (例如从 effmeet/device/audio/node1 提取 node1)
            device_id = topic.split('/')[-1]
            
            if self.audio_stream:
                self.audio_stream.add_chunk(device_id, payload)
                
        except Exception as e:
            print(f"[MQTT] 消息处理错误: {e}")

    def send_command(self, action, target_node):
        """
        下发控制指令给小车
        action: 指令类型，如 "move"
        target_node: 目标节点 ID，如 "node1"
        """
        command = {
            "action": action,
            "target": target_node  # NFC 识别模式下，直接下发 ID
        }
        
        payload = json.dumps(command)
        self.client.publish(self.topic_pub, payload)
        print(f"🚀 [MQTT指令下发] {payload} -> {self.topic_pub}")