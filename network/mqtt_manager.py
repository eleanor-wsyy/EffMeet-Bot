import paho.mqtt.client as mqtt
import json

class MQTTManager:
    def __init__(self, config):
        self.broker = config['mqtt']['broker']
        self.port = config['mqtt']['port']
        self.client_id = config['mqtt']['client_id']
        self.topic_sub = config['mqtt']['topic_sub']
        self.topic_pub = config['mqtt']['topic_pub']
        
        # 初始化客户端
        self.client = mqtt.Client(self.client_id)
        
        # 绑定回调函数
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"[MQTT] 成功连接到服务器: {self.broker}")
            # 连接成功后立刻订阅音频主题
            self.client.subscribe(self.topic_sub)
            print(f"[MQTT] 已订阅主题: {self.topic_sub}")
        else:
            print(f"[MQTT] 连接失败，错误码: {rc}")

    def _on_message(self, client, userdata, msg):
        # 组员 B 发来的数据会在这里触发
        topic = msg.topic
        payload = msg.payload
        
        # TODO: 这里之后要把 payload 塞进 audio_buffer，现在先简单打印
        print(f"\n[收到数据] 主题: {topic} | 数据长度: {len(payload)} 字节")
        
        # 如果是测试的字符串，可以尝试解码打印
        try:
            text = payload.decode('utf-8')
            print(f"[内容解析] {text}")
        except:
            print("[内容解析] (接收到二进制音频流或非文本数据)")

    def _on_disconnect(self, client, userdata, rc):
        print("[MQTT] 已断开连接，尝试重连...")

    def start(self):
        """启动 MQTT 监听线程"""
        print("[MQTT] 正在初始化通信链路...")
        self.client.connect(self.broker, self.port, 60)
        # loop_start() 会在后台开启一个独立线程处理网络收发，不会阻塞主程序
        self.client.loop_start() 

    def send_command(self, action, target_color):
        """给小车发送控制指令"""
        command = {
            "action": action,         # 例如 "move", "stop", "spin"
            "target": target_color    # 例如 "blue", "red"
        }
        payload = json.dumps(command)
        self.client.publish(self.topic_pub, payload)
        print(f"[MQTT指令下发] {payload} -> {self.topic_pub}")