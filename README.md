# EffMeet-Bot

EffMeet-Bot 是一套面向 4 人会议实验的本地系统：Windows 电脑同时采集 4 路独立麦克风、累计每人的发言时长并判断是否需要干预；ESP32-S3 机器人通过 MQTT 接收任务，移动到对应座位并显示专注、提醒、好奇或稳定表情。

当前版本：**EffMeet V1.1.1 / `v1.1.1`**
下载地址：[GitHub Releases](https://github.com/eleanor-wsyy/EffMeet-Bot/releases/latest)

> 部署提示：Release 便携版不需要 Python。运行源码时，必须先在目标电脑创建自己的 `cloud_brain/.venv`；不要复制本机的 `.venv`，也不要让脚本静默使用 Anaconda 或其他系统 Python。

## 先记住四件事

1. 双击“启动实验控制台”只会让后台进入就绪状态，**不会录音**。
2. 只有 4 路麦克风、MQTT、机器人全部在线且机器人空闲时，才能正式开始。
3. 一组实验必须有明确的 `START` 和 `END`；不能通过拔线或强制关闭后台代替结束。
4. 只有看到 `EXPERIMENT ENDED AND VERIFIED`，并且最终目录自动打开，才算实验成功。

## 部署前置检查

- Windows 10/11 x64；源码部署需要 Python 3.10 或更高版本。
- 4 只 USB 麦克风、固定集线器端口，以及管理员权限（首次改名时使用）。
- 机器人网络为 2.4 GHz Wi-Fi，电脑可以访问 MQTT Broker 的 TCP 1883 端口。
- 录音目标盘至少预留 2 GB；四路录音约每分钟 7.7 MB，收尾阶段会暂存和成品同时存在。

## 现场快速开始

### 1. 连接并固定四个麦克风

四只 USB 麦克风必须使用固定的集线器端口，并在 Windows“声音 → 录制”中命名为：

| 集线器端口 | Windows 设备名 | 对应座位 |
| --- | --- | --- |
| USB1 | `NODE1_MIC` | `node1` |
| USB2 | `NODE2_MIC` | `node2` |
| USB3 | `NODE3_MIC` | `node3` |
| USB4 | `NODE4_MIC` | `node4` |

同一只麦换到不同 USB 端口后，Windows 可能恢复成“麦克风”，或者继承该端口以前的名字。正式实验前必须重新核对，不能只看实体标签。

改名后关闭声音属性窗口，等待约 5 秒，再重新启动 EffMeet 后台。用便携版自检：

```powershell
EffMeet_App\check_mics.exe --seconds 3 --verbose
```

源码环境也可以运行：

```powershell
cd cloud_brain
python -m tools.check_mics --seconds 3 --verbose
```

只有结论为“4 路全部识别且能采集声音”时才能继续。

### 2. 启动实验控制台

双击仓库根目录的：

```text
启动实验控制台.bat
```

浏览器打开 <http://127.0.0.1:5000/>。此时后台已就绪，但实验尚未开始、麦克风尚未录音。

控制台必须同时显示：

- 4/4 麦克风在线；
- MQTT 已连接；
- 机器人在线；
- 机器人空闲；
- 保存路径可写。

### 3. 明确开始

正式实验双击：

```text
开始实验.bat
```

依次完成：

1. 确认录音目标路径；留空则使用 `Documents\EffMeet_Recordings`。
2. 输入组号；留空则按当天已有目录自动递增。
3. 输入大写 `START`。
4. 看到 `EXPERIMENT STARTED: <实验编号>` 后，才宣布实验开始。

只测试四个麦克风、不连接机器人时，可用 `开始实验-仅麦克风.bat`。该入口不是正式机器人实验入口。

### 4. 明确结束

实验结束时双击：

```text
结束实验.bat
```

核对实验编号和时长，输入大写 `END`。系统随后按顺序执行：

```text
停止接收音频
  -> 等待处理队列清空
  -> 封口四个 WAV
  -> 检查帧数、时长和数字静音
  -> 写入 session 与 manifest
  -> 复制到目标路径的 .partial 目录
  -> 逐文件核对大小和 SHA-256
  -> 原子改名为正式目录
  -> 删除本机暂存
  -> 自动关闭后台
```

任何一步失败，后台都不会把该组标记为成功，也不会删除暂存数据。

## 实验状态机制

```text
ready       后台就绪，不录音
recording   已明确开始，四路同步采集
finalizing  已明确结束，正在封口和完整性检查
exporting   正在复制并校验目标文件
exported    已完成且已验证
error       录音完整性失败，暂存保留
export_failed  录音完整，但传送失败，可重试传送
```

后台启动不等于实验开始；实验结束按钮也不等于已经成功。最终状态必须是 `exported`。

## 当前干预机制

### 座位映射

| 节点 | MQTT 目标编号 | 机器人方向 |
| --- | --- | --- |
| `node1` | `1` | 前 |
| `node2` | `2` | 左 |
| `node3` | `3` | 后 |
| `node4` | `4` | 右 |

### 检查与判定

云端默认每 120 秒检查一次当前实验组的累计发言时长：

1. 若机器人正在移动，本轮跳过。
2. 若四人总发言时长不超过 5 秒，不移动，显示稳定表情。
3. 否则计算平均值 `average = total / 4`。
4. 干预阈值为 `threshold = average × 0.5`。
5. 找到累计发言最少的人。
6. 若最低发言时长 `< threshold`，触发移动干预。
7. 若最低发言时长 `>= threshold`，不移动，显示稳定表情。

发言时长在一组实验内持续累计；明确开始下一组时，发言时长和干预计数归零。

### 同一个人的分级干预

干预计数按参会者分别保存，不是全局共用：

| 同一人在当前实验组中的干预次数 | 到达后表情 | MQTT payload |
| --- | --- | --- |
| 第 1 次 | 提醒 | `move:N:reminder` |
| 第 2 次 | 好奇 | `move:N:curious` |
| 第 3 次及以后 | 好奇 | `move:N:curious` |

例如 `node3` 第一次被干预，不会改变 `node2` 的计数。只有移动指令成功进入调度队列时才保留这次计数；发送失败会回滚。

### 四种表情

| 表情 | 使用时机 | 持续逻辑 |
| --- | --- | --- |
| 专注 `focus` | 开机、等待、移动、返程 | 默认表情 |
| 提醒 `reminder` | 同一人第 1 次被干预，到达座位后 | 4 秒后恢复专注 |
| 好奇 `curious` | 同一人第 2 次及以后被干预，到达座位后 | 4 秒后恢复专注 |
| 稳定 `stable` | 本轮没有触发移动干预 | 4 秒后恢复专注 |

一句话概括：**等待显示专注；第一次干预显示提醒；同一人第二次及以后显示好奇；没有触发干预显示稳定。**

## MQTT 协议

默认 Broker：`broker.emqx.io:1883`

| 方向 | Topic | 用途 |
| --- | --- | --- |
| 云端 → 机器人 | `esp32s3/control` | 移动和表情指令 |
| 机器人 → 云端 | `esp32s3/status` | 在线、确认、完成和错误状态 |
| 云端 → 上层系统 | `effmeet/cycle/done` | 一轮干预完成通知 |

### 移动指令

```text
move:<目标编号>:<到达后表情>
```

示例：

```text
move:1:reminder
move:2:curious
```

目标只能是 `1` 到 `4`；移动表情只能是 `reminder` 或 `curious`。兼容旧格式 `move:N` 和纯数字 `N`，它们默认使用提醒表情。

### 单独切换表情

```text
expr:focus
expr:reminder
expr:curious
expr:stable
```

机器人忙碌时不会接受新的移动任务。`stable`、`reminder`、`curious` 显示 4 秒后恢复专注。

### 常见状态回包

```text
online
expr_ack|expression=focus|frame=12
done|dir=2|target=3|expression=reminder
error|stage=track|reason=timeout
```

云端会核对 `done` 中的目标和表情，过期或不匹配的回包不能完成当前任务。

## 目录结构

```text
EffMeet-Bot/
├─ cloud_brain/                    # Windows 云端源码
│  ├─ main_brain.py                # 推荐主程序与 Web/API/MQTT 调度
│  ├─ experiment_recording.py      # 四路录音、完整性检查和传送
│  ├─ windows_setup.py             # Windows Wi-Fi 配网与恢复
│  ├─ main.py                      # 模块化备用入口
│  ├─ config.yaml                  # 模块化入口配置
│  ├─ requirements.txt             # 基础源码依赖（跨电脑部署）
│  ├─ requirements-ml.txt          # 可选 Faster-Whisper / Silero VAD 依赖
│  ├─ core/                        # 音频与人声判定
│  ├─ logic/                       # 会议状态和干预逻辑
│  ├─ network/                     # MQTT 封装
│  ├─ utils/                       # 音频缓冲和报表工具
│  ├─ templates/                   # 实验控制台页面
│  ├─ tools/                       # 麦克风与状态诊断工具
│  ├─ tests/                       # 自动测试和硬件验收脚本
│  └─ packaging/                   # PyInstaller 配置与精简依赖
├─ robot_esp32/
│  └─ 1.3/                         # Arduino 草图；目录名必须与 1.3.ino 匹配
├─ EffMeet_App/                    # 已打包的 Windows 便携版
├─ scripts/                        # 部署、启动、结束和固件构建脚本
├─ 首次部署.bat                    # 为当前电脑创建 cloud_brain/.venv
├─ 启动实验控制台.bat
├─ 开始实验.bat
├─ 开始实验-仅麦克风.bat
├─ 结束实验.bat
└─ README.md
```

顶层公共路径保持不变，避免破坏现有便携版、Arduino 烧录脚本和同学已经保存的快捷方式。

## Windows 便携版与源码运行

### 便携版

下载 Release 后必须完整解压，不能只复制 `EffMeet.exe`。程序依赖同目录下的 `_internal`：

```text
EffMeet_App/
├─ EffMeet.exe
├─ check_mics.exe
└─ _internal/
```

仓库中的 `cloud_brain/.venv` 只属于创建它的电脑，不能随压缩包复制到其他电脑运行。跨电脑使用有两种可靠方式：

1. **便携版（推荐）**：完整解压 Release 的 `EffMeet_App/`，直接双击“启动实验控制台.bat”。
2. **源码版**：在目标电脑双击“首次部署.bat”。脚本会检测 Python、创建本机 `cloud_brain/.venv` 并安装基础依赖；以后启动脚本只会使用这个虚拟环境。

### 源码运行

```powershell
# 首次部署（也可以双击仓库根目录的“首次部署.bat”）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_env.ps1

# 可选：安装语音转写和 Silero VAD；不安装也不影响录音、发言判定和 MQTT 干预
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_env.ps1 -WithMl

# 直接使用本机虚拟环境启动
cd cloud_brain
.\.venv\Scripts\python.exe main_brain.py
```

如果缺少 `faster-whisper` 或 `torch`，程序会明确提示并降级：核心录音、robust 发言判定和 MQTT 干预继续工作，只有语音转写或 Silero VAD 不可用。手动启动 `main_brain.py` 后仍然只是就绪状态，必须在控制台或 API 中明确开始实验。

### 重新打包

在常规 Python 环境中运行：

```powershell
cd cloud_brain
python -m pip install pyinstaller -r packaging\requirements_pack.txt
python -m PyInstaller packaging\EffMeet.spec --noconfirm --clean
```

输出位于 `cloud_brain/dist/EffMeet/`。验证 `EffMeet.exe` 和 `check_mics.exe` 后，再整体替换仓库根目录的 `EffMeet_App/`。

打包版默认不含 Faster-Whisper 和 Torch，但保留四路录音、人声判定、干预调度和 Wi-Fi 配网。

## 机器人固件

固件入口：`robot_esp32/1.3/1.3.ino`

### 编译与烧录

```powershell
# 只编译
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_firmware.ps1

# 编译并烧录；必须明确串口，避免烧错设备
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_firmware.ps1 -Upload -Port COM7
```

脚本使用固定英文路径 `C:\EffMeetBuild`，兼容中文 Windows 用户名并复用 Arduino 缓存。日常更换 Wi-Fi 不需要重新烧录。

### 当前接线定义

TFT 8 位并口：

| TFT | ESP32-S3 |
| --- | --- |
| `DC` | GPIO36 |
| `CS` | GPIO35 |
| `WR` | GPIO37 |
| `RD` | GPIO38 |
| `RST` | GPIO42 |
| `D0`–`D7` | GPIO2、3、8、9、10、11、12、18 |

注意：`D7` 当前是 **GPIO18**，GPIO19 已废弃；`RST` 当前是 **GPIO42**。

L298N：

| L298N | ESP32-S3 | 电机 |
| --- | --- | --- |
| `IN1` / `IN2` | GPIO21 / GPIO47 | 右轮方向 |
| `ENA` | GPIO40 | 右轮 PWM |
| `IN3` / `IN4` | GPIO14 / GPIO13 | 左轮方向 |
| `ENB` | GPIO41 | 左轮 PWM |

五路循迹传感器 `FL / ML / MID / MR / FR` 分别接 GPIO4 / 5 / 6 / 7 / 15，计数传感器接 GPIO1。所有模块必须共地。

## Wi-Fi 配置

Wi-Fi 凭据保存在 ESP32 NVS 中，不写死在固件。更换场地网络时：

1. 启动 EffMeet 控制台。
2. 在“设备连接向导”中扫描附近 Wi-Fi。
3. 选择机器人热点 `EffMeet-Setup-XXXX`。
4. 填写目标 2.4 GHz Wi-Fi 和密码。
5. 点击“连接机器人并自动恢复电脑网络”。

程序会临时连接机器人热点、把凭据发送给本地机器人、恢复电脑原网络，再等待 MQTT 和机器人上线。密码不会写入仓库或日志。

手动配网备用方式：连接 `EffMeet-Setup-XXXX`，热点密码 `EffMeet123`，浏览器打开 <http://192.168.4.1/>。

ESP32 只支持 2.4 GHz。校园网页认证、账号认证或证书网络不能只靠 SSID 和密码完成连接；防火墙还可能单独拦截 MQTT TCP 1883。

## 录音目录与文件

默认最终目录：

```text
C:\Users\<用户名>\Documents\EffMeet_Recordings\
└─ YYYYMMDD_HHMMSS_groupNNN\
   ├─ YYYYMMDD_HHMMSS_groupNNN_node1.wav
   ├─ YYYYMMDD_HHMMSS_groupNNN_node2.wav
   ├─ YYYYMMDD_HHMMSS_groupNNN_node3.wav
   ├─ YYYYMMDD_HHMMSS_groupNNN_node4.wav
   ├─ YYYYMMDD_HHMMSS_groupNNN_session.json
   └─ YYYYMMDD_HHMMSS_groupNNN_manifest.json
```

源码版暂存通常位于 `cloud_brain/data/recording_staging/`；便携版暂存位于 `EffMeet_App/_internal/data/recording_staging/`。这些目录包含参与者声音，不应提交 GitHub。

### 失败处理

- `export_failed`：四路录音已完整封口，但目标盘或网络路径传送失败；修复路径后使用控制台“重试传送”。
- `error`：录音本身不完整，例如某路没有音频、长期数字静音、帧数明显不一致、四路在实验结束前提前停止；不能标记成功，也不能用传送重试掩盖。
- 任何失败都先记录控制台显示的 `staging_dir`，不要删除暂存、不要覆盖同名目标目录。

## 测试

在 `cloud_brain` 目录运行自动测试：

```powershell
python -m tests.test_activity_engine
python -m tests.test_intervention_expressions
python -m tests.test_experiment_recording
python -m tests.test_experiment_lifecycle
python -m tests.test_session_management
python -m tests.test_device_setup
```

现场硬件测试：

```powershell
# 四路麦克风逐路采样
python -m tools.check_mics --seconds 3 --verbose

# 只检查四种表情
python -m tests.test_hardware_stability --expression-only

# 表情预检 + 1→2→3→4→1 共五次往返
python -m tests.test_hardware_stability
```

MQTT 调度联调：

```powershell
python -m tests.test_dispatch 3 2
```

软件测试不能代替现场试听。正式采集前必须做一段短录音，正常结束并逐个试听 `node1` 到 `node4`。

## 本地 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 四麦、MQTT、机器人和实验状态 |
| `GET` | `/api/get_meeting_data` | 发言时长、判定、干预和录音详情 |
| `POST` | `/api/experiment/start` | 明确开始 |
| `POST` | `/api/experiment/end` | 明确结束并传送 |
| `POST` | `/api/experiment/retry-export` | 重试已完整录音的传送 |
| `GET` | `/api/setup/networks` | 扫描 Wi-Fi |
| `POST` | `/api/setup/provision` | 给机器人配置 Wi-Fi |

开始请求示例：

```json
{
  "output_dir": "D:\\EffMeet_Recordings",
  "group_number": null,
  "require_robot": true
}
```

正式现场优先使用 `.bat` 或网页控制台，不建议手写 API 请求绕过确认步骤。

## 常见问题

### 改名后仍只显示 0/4 或 2/4 麦克风

1. 关闭 Windows 声音属性窗口。
2. 确认四个名称精确为 `NODE1_MIC` 到 `NODE4_MIC`。
3. 等待 5 秒。
4. 结束未录音的旧后台并重新启动。
5. 再运行 `check_mics.exe --verbose`。

PortAudio 会缓存启动时的设备表；在后台运行期间改名，旧进程不一定立刻识别新名称。

### 提示“四路录音均早于实验结束停止”

该组已经停止，但没有通过完整性检查。常见原因是 USB 集线器瞬断、供电不足、接头松动或录音进程异常。保留 `staging_dir`，检查日志和 WAV 时长，修复硬件后重新做短试录；不能把该组当成正式数据。

### `EffMeet.exe` 打不开

- 必须完整解压 `EffMeet_App`，不能单独移动 EXE。
- 路径中不能缺少 `_internal`。
- 查看 `cloud_brain/data/logs/backend-*-error.log`。
- 不要使用其他电脑打包进 ZIP 的 `.venv`。

### MQTT 在线但机器人离线

确认电脑和机器人使用相同 Broker/Topic，机器人已连 2.4 GHz Wi-Fi，并检查网络是否允许 TCP 1883。给机器人重新上电后等待约 30 秒。

### TFT 半屏、花屏或移动后不完整

先确认 `D7 → GPIO18`、`RST → GPIO42`、屏幕与电机共地。若问题只在电机启停时出现，重点检查屏幕供电压降、充电宝输出、接头松动和电机干扰抑制。软件会在电机停止后复位并重绘，但不能补偿持续掉电。

## 每组实验前后检查表

开始前：

- [ ] 四只麦固定在 USB1–USB4，Windows 名称和座位一致。
- [ ] `check_mics` 四路都有声音。
- [ ] MQTT、机器人在线，机器人空闲。
- [ ] TFT 完整显示专注表情。
- [ ] 保存路径可写且空间足够。
- [ ] 控制台仍为 `ready`，没有上组残留录音。

结束后：

- [ ] 输入 `END` 后看到 `EXPERIMENT ENDED AND VERIFIED`。
- [ ] 最终目录自动打开。
- [ ] 四个 WAV 都存在，时长接近。
- [ ] `manifest.json` 与 `session.json` 存在。
- [ ] 抽听四路，确认座位映射和音量正常。
