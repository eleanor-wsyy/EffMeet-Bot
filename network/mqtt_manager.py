import paho.mqtt.client as mqtt
import json
from utils.audio_buffer import AudioStreamManager

class MQTTManager:
    def __init__(self, config, audio_stream_manager):
        self.broker = config['mqtt']['broker']
        self.port = config['mqtt']['port']
        self.client_id = config['mqtt']['client_id']
        self.topic_sub = config['mqtt']['topic_sub']
        self.topic_pub = config['mqtt']['topic_pub']
        
        # 挂载音频流水线拼接器
        self.audio_stream = audio_stream_manager
        
        # 初始化客户端
        self.client = mqtt.Client(self.client_id)
        
        # 绑定回调函数
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"[MQTT] 成功连接到服务器: {self.broker}")
            self.client.subscribe(self.topic_sub)
            print(f"[MQTT] 已订阅主题: {self.topic_sub}")
        else:
            print(f"[MQTT] 连接失败，错误码: {rc}")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload
        
        # 提取设备ID (假设主题格式为 effmeet/device/audio/node1，切割后取最后的 node1)
        try:
            device_id = topic.split('/')[-1]
            
            # 把收到的碎片数据直接扔进流水线拼接器
            self.audio_stream.add_chunk(device_id, payload)
            # 👇👇👇 [新增的临时物理外挂] 👇👇👇
            # 只要收到 MQTT 消息，不管是不是人声，强行给这个人加 20 秒发言时间！
            self.audio_stream.meeting_state.add_speech_time(device_id, 20.0)  # 直接加上
            # 👆👆👆-------------------------👆👆👆
            
            # 为了调试不刷屏，这里暂时屏蔽了每次收到碎片的打印
            print(f"[MQTT接收] {device_id} 发来 {len(payload)} 字节")
            
        except Exception as e:
            print(f"[MQTT] 解析数据包错误: {e}")

    def _on_disconnect(self, client, userdata, rc):
        print("[MQTT] 已断开连接，尝试重连...")

    def start(self):
        """启动 MQTT 监听线程"""
        print("[MQTT] 正在初始化通信链路...")
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_start() 

    def send_command(self, action, target_color):
        """给小车发送控制指令"""
        command = {
            "action": action,         
            "target": target_color    
        }
        payload = json.dumps(command)
        self.client.publish(self.topic_pub, payload)
        print(f"[MQTT指令下发] {payload} -> {self.topic_pub}")