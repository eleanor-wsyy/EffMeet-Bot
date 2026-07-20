# EffMeet-Bot

EffMeet-Bot 是一套用于会议发言统计与干预调度的桌面级系统，当前仓库主要由两部分组成：

- `cloud_brain/`：Python 云端程序，负责采集 4 路 USB 麦克风、做人声判断、统计发言时长、通过 MQTT 下发机器人调度指令，并提供本地状态接口。
- `robot_esp32/`：ESP32-S3 机器人端固件，负责接收 MQTT 指令后按目标方向巡线移动，完成后回传状态。

当前仓库里可直接运行的主入口是 `cloud_brain/main_brain.py` 和 `robot_esp32/1.3/1.3.ino`。

## 项目目标

这个项目的核心思路是：

1. 云端从 4 路麦克风中判断当前谁在发言。
2. 云端累计每个节点的发言时长。
3. 如果发言分布明显失衡，云端通过 MQTT 通知 ESP32-S3 机器人移动到对应方向。
4. 机器人完成动作后回传 `done`，云端再决定是否继续下一轮干预。

## 目录结构

```text
EffMeet-Bot-main/
├─ cloud_brain/
│  ├─ main_brain.py        # 当前推荐的云端主程序
│  ├─ main.py              # 模块化版本入口，适合二次开发
│  ├─ config.yaml          # MQTT 和调度参数
│  ├─ requirements.txt     # Python 依赖
│  ├─ check_status.py      # 终端状态查看器
│  ├─ list_mics.py         # 列出本机输入设备
│  ├─ core/
│  │  ├─ vad_engine.py     # Silero VAD 封装
│  │  └─ speaker_id.py     # 预留模块
│  ├─ logic/
│  │  ├─ meeting_state.py  # 会议统计与调度逻辑
│  │  └─ commander.py      # 预留模块
│  ├─ network/
│  │  └─ mqtt_manager.py   # MQTT 封装
│  ├─ utils/
│  │  ├─ audio_buffer.py   # 音频缓冲与 VAD 过滤
│  │  └─ report_gen.py     # Excel 报表生成
│  └─ test_*.py            # 联调和单元测试脚本
├─ robot_esp32/
│  └─ 1.3/
│     ├─ 1.3.ino           # ESP32-S3 机器人固件
│     ├─ image_array.h     # TFT 开机图像数据
│     └─ User_Setup.h      # TFT 配置参考
├─ README.md
└─ READ.me                 # 兼容入口，指向 README.md
```

## 云端功能

`cloud_brain/main_brain.py` 负责：

- 自动识别重命名为 `NODE1_MIC` 到 `NODE4_MIC` 的 4 路 USB 麦克风。
- 按固定采样窗口统计每一路的分贝、底噪和相对得分。
- 结合 Silero VAD 判断是否为有效人声。
- 累计每个节点的发言时长。
- 当某个节点明显低于均值时，通过 MQTT 下发机器人目标方向。
- 在 `Ctrl+C` 退出时尝试生成 Excel 会议报表。
- 提供 Flask 接口，方便实时查看当前统计结果。

### 当前主逻辑

默认调度策略是：

- 总发言时长小于等于 5 秒时不触发干预。
- 以 4 人平均发言时长为基准。
- 若最低发言者低于 `平均值 * 0.5`，则触发一次干预。
- 每位参会者独立累计干预次数：第 1 次显示提醒表情，第 2 次及以后显示好奇表情。
- 未触发干预时显示稳定表情 4 秒，随后自动恢复专注表情。
- 干预次数按当前程序会话累计，重新启动或重置会话后归零。

## 机器人端功能

`robot_esp32/1.3/1.3.ino` 负责：

- 连接 Wi-Fi 和 MQTT Broker。
- 订阅 `esp32s3/control`，接收移动与表情指令。
- 控制 L298N 电机驱动，按目标方向巡线前进。
- 利用 5 路循迹传感器和 1 路计数传感器完成转向、到点、回程。
- 等待和移动期间显示专注表情，到达目标后按干预次数显示提醒或好奇表情。
- 执行完毕后向 `esp32s3/status` 发布包含方向、目标和表情的 `done` 回包。

## 运行环境

### 云端

- Windows 10 / 11
- Python 3.10+，建议使用独立虚拟环境
- 4 路 USB 麦克风
- 网络可访问 MQTT Broker

### 机器人端

- ESP32-S3 开发板
- L298N 电机驱动模块
- 左右直流减速电机
- 5 路循迹传感器
- 1 路红外计数传感器
- 3.5 英寸 TFT LCD，当前固件使用 ILI9488

## Python 依赖

`cloud_brain/requirements.txt` 中包含的主要依赖：

- `paho-mqtt`
- `numpy`
- `pydub`
- `pandas`
- `openpyxl`
- `PyYAML`
- `sounddevice`
- `Flask`
- `Werkzeug`
- `faster-whisper`
- `torch`

## 快速开始

### 1. 配置麦克风名称

在 Windows 录音设备里，把 4 个输入设备重命名为：

```text
NODE1_MIC
NODE2_MIC
NODE3_MIC
NODE4_MIC
```

程序会按这 4 个名字自动绑定到 `node1` 到 `node4`。

### 2. 安装云端依赖

```powershell
cd cloud_brain
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. 先检查麦克风

```powershell
cd cloud_brain
python list_mics.py
```

确认系统能看到 4 个输入设备，且名称中包含 `NODE1_MIC` 到 `NODE4_MIC`。

### 4. 启动 ESP32-S3

先把 `robot_esp32/1.3/1.3.ino` 烧录到板子上，并确认：

- Wi-Fi 已配置正确
- MQTT Broker 可连接
- 串口输出显示已连上网络和 MQTT

### 5. 启动云端主程序

```powershell
cd cloud_brain
python main_brain.py
```

启动成功后会看到类似提示：

```text
[READY] 系统全部就绪，等待发言数据...
```

### 6. 查看实时状态

另开一个终端：

```powershell
cd cloud_brain
python check_status.py
```

它会轮询本地接口 `http://127.0.0.1:5000/api/get_meeting_data`，在终端里显示：

- 各节点累计发言时长
- 最近一次音频判定结果
- 最近几条计时事件

## 启动参数

`cloud_brain/main_brain.py` 支持一些调试参数：

```powershell
python main_brain.py --smoke
python main_brain.py --no-mic
python main_brain.py --loose-thresholds
python main_brain.py --schedule-interval 60
python main_brain.py --demo-state node1=100,node2=10,node3=0,node4=15
```

参数说明：

- `--smoke`：只检查麦克风是否可识别，不启动 Web / MQTT / 音频线程。
- `--no-mic`：不打开麦克风流，仍保留 Web 和 MQTT 调度。
- `--loose-thresholds`：放宽分贝阈值，适合现场调试。
- `--schedule-interval`：修改调度检查间隔，单位秒。
- `--demo-state`：手动注入各节点发言时长，便于模拟调度。

## MQTT 协议

默认使用公开 EMQX Broker：

```text
broker.emqx.io:1883
```

主题定义如下：

| 方向 | 主题 | 说明 |
| --- | --- | --- |
| 云端 -> 机器人 | `esp32s3/control` | 下发移动或表情指令 |
| 机器人 -> 云端 | `esp32s3/status` | 机器人完成后回传任务状态 |
| 云端 -> 上层系统 | `effmeet/cycle/done` | 一轮干预结束后发布 `cycle_done` |

控制 payload：

| 时机 | payload | 机器人行为 |
| --- | --- | --- |
| 等待下一轮检查 | `expr:focus` | 持续显示专注 |
| 同一人第 1 次干预 | `move:N:reminder` | 专注状态移动，到达后显示提醒 4 秒，再恢复专注并返程 |
| 同一人第 2 次及以后干预 | `move:N:curious` | 专注状态移动，到达后显示好奇 4 秒，再恢复专注并返程 |
| 本轮没有触发干预 | `expr:stable` | 显示稳定 4 秒，然后自动恢复专注 |
| 单独测试表情 | `expr:reminder` / `expr:curious` | 直接切换到指定表情，直到收到下一条表情或移动指令 |

`N` 为 `1` 到 `4` 的座位方向。旧版纯数字 payload `1` / `2` / `3` / `4`
仍兼容，并按 `move:N:reminder` 处理。

任务完成回包示例：

```text
done|dir=3|target=1|expression=curious
```

方向编号约定：

```text
1 = 前
2 = 左
3 = 后
4 = 右
```

## 本地接口

云端启动后会开放 Flask 接口：

```text
GET http://127.0.0.1:5000/api/get_meeting_data
```

返回内容包含：

- `current_speaking_times`
- `total_speaking_time`
- `latest_records`
- `latest_speaking_events`
- `latest_audio_state`

## 测试脚本

`cloud_brain/` 下带了几份联调脚本：

- `test_local_mic.py`：测试本地麦克风和 VAD。
- `test_multi_mic.py`：测试 4 路麦克风输入与统计。
- `test_dispatch.py`：模拟机器人回包，验证 MQTT 干预链路。
- `test_intervention_expressions.py`：验证同一参会者的提醒/好奇分级干预与稳定表情逻辑。
- `test_meeting_logic.py`：验证会议状态和调度逻辑。

示例：

```powershell
cd cloud_brain
python test_dispatch.py
python test_intervention_expressions.py
```

## 代码说明

### `cloud_brain/main_brain.py`

这是当前仓库里最完整的云端主程序，核心流程是：

1. 识别麦克风设备。
2. 启动 Flask、Whisper、MQTT 调度和音频分析线程。
3. 采集 4 路音频。
4. 用分贝、底噪、VAD 和领先差综合判断发言者。
5. 累计发言时长。
6. 在发言分布失衡时触发机器人干预。

### `cloud_brain/main.py`

这是一个更模块化的入口，内部拆分了：

- `logic/meeting_state.py`
- `network/mqtt_manager.py`
- `utils/audio_buffer.py`
- `utils/report_gen.py`

如果你想继续把项目往可维护方向重构，可以优先看这一套。

### `robot_esp32/1.3/1.3.ino`

这是机器人固件当前版本，负责：

- 订阅控制主题
- 巡线转向
- 到点停留
- 回到起点
- 回传完成状态

## 注意事项

- 公开 MQTT Broker 适合演示和联调，不建议长期生产使用。
- 首次运行 `faster-whisper` 和 `torch` 时可能需要下载模型缓存。
- 如果某一路麦克风长期偏大或偏小，可以在 `cloud_brain/main_brain.py` 里调整 `MIC_GAIN_OFFSETS_DB`。
- `cloud_brain/core/speaker_id.py` 和 `cloud_brain/logic/commander.py` 当前属于预留模块。

## 推荐工作流

1. 先烧录机器人端固件。
2. 再给 4 路麦克风重命名。
3. 安装 Python 依赖。
4. 运行 `python list_mics.py` 确认设备正常。
5. 启动 ESP32-S3。
6. 启动 `python main_brain.py`。
7. 用 `python check_status.py` 观察实时统计。
