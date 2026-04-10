import queue
import threading
from core.vad_engine import VADEngine

class AudioStreamManager:
    def __init__(self, meeting_state, chunk_duration_sec=1.0, sample_rate=16000, bit_depth=16):
        """
        初始化音频流管理器
        """
        self.meeting_state = meeting_state
        self.chunk_duration_sec = chunk_duration_sec
        self.target_bytes = int(sample_rate * (bit_depth / 8) * chunk_duration_sec)
        
        self.buffers = {}
        self.ai_task_queue = queue.Queue()
        self.lock = threading.Lock()
        
        # 启动 VAD 过滤器
        self.vad = VADEngine(sample_rate=sample_rate)

    def add_chunk(self, device_id: str, payload: bytes):
        """接收碎片的音频包并拼接"""
        with self.lock:
            # 如果是新设备，给它开个新户头
            if device_id not in self.buffers:
                self.buffers[device_id] = bytearray()
            
            # 把收到的碎片加进去
            self.buffers[device_id].extend(payload)
            
            # 如果攒够了目标长度，就切下来打包
            if len(self.buffers[device_id]) >= self.target_bytes:
                # 切下完整的一块
                full_frame = self.buffers[device_id][:self.target_bytes]
                
                # [关键拦截]：用 VAD 模型测一下这段音频有没有人说话
                if self.vad.is_speech(bytes(full_frame)):
                    # 通知大脑：这个人刚才说了一段话！给他加时长！
                    self.meeting_state.add_speech_time(device_id, self.chunk_duration_sec)
                    
                    # 扔进任务队列给后续可能接入的 3D-Speaker 留后路
                    self.ai_task_queue.put((device_id, bytes(full_frame)))
                else:
                    # 没人说话，直接把这段音频像垃圾一样丢弃，节省算力
                    pass 
                
                # 剩下的部分保留，继续攒
                self.buffers[device_id] = self.buffers[device_id][self.target_bytes:]