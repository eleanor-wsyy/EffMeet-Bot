import sounddevice as sd
devices = sd.query_devices()
print("=== 当前音频输入设备列表 ===")
for i, d in enumerate(devices):
    if d['max_input_channels'] > 0:
        print(f"[{i}] {d['name']} | 输入通道: {d['max_input_channels']}")
