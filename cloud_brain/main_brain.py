# -*- coding: utf-8 -*-
import io
import argparse
import json
import os
import queue
import random
import sys
import threading
import time
import traceback
import wave
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
from flask import Flask, jsonify, render_template, request

from core.activity_engine import ActivityEngine
from core.vad_engine import VADEngine
from experiment_recording import ExperimentRecorder, RecordingError
import windows_setup


# 统一控制台编码，避免 Windows 双击运行时中文日志乱码。
if sys.platform.startswith("win"):
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass

if hasattr(sys.stdout, "buffer") and (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )
if hasattr(sys.stderr, "buffer") and (sys.stderr.encoding or "").lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )


# 基础音频参数。
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
MICROPHONE_SCAN_INTERVAL_SECONDS = 5.0
AUDIO_CALLBACK_TIMEOUT_SECONDS = 2.0
AUDIO_START_TIMEOUT_SECONDS = 2.0
CALIBRATION_SECONDS = 3.0
ABSOLUTE_DB_FLOOR = 45.0
SPEECH_ABOVE_NOISE_DB = 10.0
WINNER_MARGIN_DB = 4.0
USE_VAD = True
USE_WHISPER = True

# robust 模式下的判定参数（见 判定改进设计.md）。
SPEECH_HI_DB = 10.0          # 双门限开口高门限（相对底噪）
SPEECH_LO_DB = 6.0           # 双门限维持低门限（相对底噪）
FLOOR_ALPHA = 0.03           # 自适应底噪时间常数
VAD_MAX_SIL = 3              # 全局 VAD 静音容忍块数
DOM_HANGOVER = 3             # 主导者静音容忍块数（说话停顿不误停）
DOM_LEAD_CONFIRM = 2         # 主导者接管前需持续块数（防单块误占）
DETECT_MODE = "robust"       # 说话人判定模式：robust / legacy

# robust 判定引擎（惰性创建，避免 --smoke 时生成）。
_activity_engine = None

# 麦克风灵敏度补偿。某一路长期偏小就填正数，长期偏大就填负数。
MIC_GAIN_OFFSETS_DB = {
    "node1": 0.0,
    "node2": 0.0,
    "node3": 0.0,
    "node4": 0.0,
}

# MQTT 通信参数：云端大脑通过这些主题和机器人端同步状态。
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
MQTT_TOPIC_CONTROL = "esp32s3/control"
MQTT_TOPIC_STATUS = "esp32s3/status"
MQTT_TOPIC_CYCLE_DONE = "effmeet/cycle/done"
SILENCE_TIMEOUT = 120
IMBALANCE_RATIO_THRESHOLD = 0.5
INTERVENTION_ORDER = [1, 2, 3, 4]
ROBOT_TASK_TIMEOUT_SECONDS = 180

BASE_DIR = Path(__file__).resolve().parent
SESSION_ARCHIVE_DIR = BASE_DIR / "data" / "sessions"

# Windows 录音设备需要按这些名字重命名，程序会据此绑定 4 个麦克风。
NODE_HARDWARE_MAP = ["NODE1_MIC", "NODE2_MIC", "NODE3_MIC", "NODE4_MIC"]

# Flask API 和会议实时数据。
app = Flask(__name__)
meeting_records = []
speaking_times = {f"node{i}": 0.0 for i in range(1, 5)}
speaking_events = deque(maxlen=20)
latest_audio_state = {}
state_lock = threading.Lock()

# 机器人调度状态。
_robot_busy = False
_cycle_index = 0
_active_interventions = []
_intervention_counts = {f"node{i}": 0 for i in range(1, 5)}
_cycle_lock = threading.Lock()
_mqtt_client_ref = None
_mqtt_connected = threading.Event()
_robot_online = threading.Event()
_scheduler_reset_event = threading.Event()
_robot_task_started_at = 0.0
_next_schedule_check_at = 0.0
_whisper_model = None

# 实验分组状态。每次点击“结束并开始下一组”都会归档当前数据并递增 generation，
# 后台尚未完成的旧语音转写会据此被丢弃，避免串入下一组。
_session_generation = 0
_session_id = ""
_session_started_at = ""
_last_archive_path = ""

# 每个麦克风各自维护一个音频队列，转写线程从 transcribe_queue 里取完整句子。
audio_queues = {f"node{i}": queue.Queue() for i in range(1, 5)}
transcribe_queue = queue.Queue()
_audio_queue_lock = threading.Lock()

# 单次实验生命周期：后台就绪不等于实验已开始；只有明确点击开始后才打开音频流。
experiment_recorder = ExperimentRecorder(
    staging_root=BASE_DIR / "data" / "recording_staging",
    sample_rate=SAMPLE_RATE,
    channels=1,
    sample_width=2,
)
_experiment_active = threading.Event()
_experiment_ending = threading.Event()
_shutdown_requested = threading.Event()
_brain_flush_request = threading.Event()
_brain_flushed = threading.Event()
_stream_lock = threading.Lock()
_audio_streams = []
_microphones = {}
_microphone_lock = threading.Lock()
_last_microphone_scan_monotonic = 0.0
_microphone_scan_error = ""
_audio_health_lock = threading.Lock()
_audio_last_chunk_monotonic = {}
_audio_callback_errors = {}
_audio_workers_started = False
_no_mic_mode = False
_no_robot_mode = False
_pending_session_snapshot = None
_experiment_api_lock = threading.Lock()

# Windows 设备连接向导。Wi-Fi 密码只存在于单次后台线程参数中，不写日志或状态。
_setup_state_lock = threading.Lock()
_setup_thread = None
_setup_state = {
    "state": "idle",
    "step": "等待检查",
    "message": "",
    "error": "",
    "setup_ssid": "",
    "target_ssid": "",
    "original_ssid": "",
    "current_ssid": "",
    "started_at": "",
    "completed_at": "",
}
_connectivity_lock = threading.Lock()
_connectivity_cache = {"checked_at": 0.0, "wifi": {}, "internet_available": False, "error": ""}


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--smoke", action="store_true", help="Only check microphone discovery and exit.")
    parser.add_argument("--no-mic", action="store_true", help="Start web/MQTT without opening microphone streams.")
    parser.add_argument(
        "--loose-thresholds",
        action="store_true",
        help="Use a more permissive threshold set for live experiments.",
    )
    parser.add_argument(
        "--schedule-interval",
        type=float,
        default=SILENCE_TIMEOUT,
        help="Seconds between balance checks in MQTT scheduler.",
    )
    parser.add_argument(
        "--demo-state",
        type=str,
        default="",
        help="Seed speaking times, e.g. node1=100,node2=10,node3=0,node4=15.",
    )
    parser.add_argument(
        "--detect-mode",
        type=str,
        choices=["robust", "legacy"],
        default=DETECT_MODE,
        help="说话人判定模式：robust 使用自适应底噪+主导说话人+静音容忍；legacy 保留旧瞬时判定。",
    )
    parser.add_argument(
        "--no-whisper",
        action="store_true",
        help="不启动语音转写线程（不加载 faster-whisper / torch），仅录音+人声判定+干预。",
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="不初始化 Silero VAD（不加载 torch）。robust 判定本身不依赖 VAD。",
    )
    return parser.parse_args(argv)


def apply_demo_state(spec):
    if not spec:
        return

    updates = {}
    for part in spec.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"Invalid demo state item: {piece}")
        key, value = piece.split("=", 1)
        key = key.strip()
        if key not in speaking_times:
            raise ValueError(f"Unknown node in demo state: {key}")
        updates[key] = float(value.strip())

    with state_lock:
        for key, value in updates.items():
            speaking_times[key] = value

    print(f"[DEMO] 已注入发言时长: {updates}")


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _make_session_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _session_snapshot_locked(reason):
    """Build an archive while both state locks are held."""
    times = dict(speaking_times)
    return {
        "schema_version": 1,
        "session_id": _session_id,
        "started_at": _session_started_at,
        "ended_at": _now_iso(),
        "end_reason": reason,
        "speaking_times": times,
        "total_speaking_time": round(sum(times.values()), 3),
        "intervention_counts": dict(_intervention_counts),
        "meeting_records": [dict(item) for item in meeting_records],
        "speaking_events": [dict(item) for item in speaking_events],
        "latest_audio_state": dict(latest_audio_state),
    }


def _write_session_archive(snapshot):
    SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = SESSION_ARCHIVE_DIR / f"session_{snapshot['session_id']}.json"
    temporary_path = archive_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(archive_path)
    return archive_path


def _discard_queued_audio():
    # 丢掉按钮按下前已进入采集队列、但尚未完成分析的短音频块。
    for audio_queue in audio_queues.values():
        while True:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break


def reset_runtime_state(
    archive_current=False,
    reason="startup",
    require_idle=False,
    session_id=None,
    started_at=None,
):
    """Archive the current group, then atomically start a clean experiment group."""
    global _robot_busy, _cycle_index, _active_interventions
    global _robot_task_started_at, _session_generation
    global _session_id, _session_started_at, _last_archive_path

    archive_path = None
    with _cycle_lock:
        if require_idle and _robot_busy:
            raise RuntimeError("机器人仍在执行任务，请等待它返回起点后再开始下一组。")

        with state_lock:
            if archive_current and _session_id:
                archive_path = _write_session_archive(
                    _session_snapshot_locked(reason)
                )

            meeting_records.clear()
            latest_audio_state.clear()
            speaking_events.clear()
            for key in speaking_times:
                speaking_times[key] = 0.0

            _robot_busy = False
            _robot_task_started_at = 0.0
            _cycle_index = 0
            _active_interventions = []
            for key in _intervention_counts:
                _intervention_counts[key] = 0

            _session_generation += 1
            _session_id = session_id or _make_session_id()
            _session_started_at = started_at or _now_iso()
            _last_archive_path = str(archive_path) if archive_path else ""

    _discard_queued_audio()
    _scheduler_reset_event.set()

    # 新实验组归零判定归属/活动状态（保留自适应底噪）。
    if _activity_engine is not None:
        _activity_engine.reset()

    client = _mqtt_client_ref
    if client is not None and client.is_connected():
        client.publish(MQTT_TOPIC_CONTROL, "expr:focus")

    if archive_path:
        print(f"[SESSION] 上一组已归档：{archive_path}")
    print(f"[SESSION] 新实验组已开始：{_session_id}")
    return {
        "session_id": _session_id,
        "started_at": _session_started_at,
        "archive_path": str(archive_path) if archive_path else None,
    }


def snapshot_runtime_state(reason):
    with _cycle_lock:
        with state_lock:
            return _session_snapshot_locked(reason)


def serialize_experiment_api(function):
    """Reject overlapping lifecycle requests instead of interleaving them."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not _experiment_api_lock.acquire(blocking=False):
            return jsonify(
                {
                    "status": "busy",
                    "message": "另一条实验开始/结束请求正在处理，请等待当前操作完成。",
                }
            ), 409
        try:
            return function(*args, **kwargs)
        finally:
            _experiment_api_lock.release()

    return wrapped


def _update_setup_state(**updates):
    with _setup_state_lock:
        _setup_state.update(updates)
        return dict(_setup_state)


def _setup_state_snapshot():
    with _setup_state_lock:
        return dict(_setup_state)


def _connectivity_snapshot(force=False):
    now = time.monotonic()
    with _connectivity_lock:
        if not force and now - _connectivity_cache["checked_at"] < 3.0:
            return dict(_connectivity_cache)
        try:
            wifi = windows_setup.connected_wifi()
            error = ""
        except windows_setup.SetupError as exc:
            wifi = {}
            error = str(exc)
        internet = windows_setup.internet_available(timeout=2)
        _connectivity_cache.update(
            checked_at=now,
            wifi=wifi,
            internet_available=internet,
            error=error,
        )
        return dict(_connectivity_cache)


def _restore_after_setup(original, setup_ssid):
    restored = windows_setup.restore_original_wifi(original)
    windows_setup.remove_setup_profile(setup_ssid, original.get("name", ""))
    _connectivity_snapshot(force=True)
    return restored


def _provision_robot_wifi(setup_ssid, target_ssid, password):
    original = {}
    setup_connected = False
    restored = False
    try:
        original = windows_setup.connected_wifi()
        if not original.get("name"):
            raise windows_setup.SetupError("没有找到可用的 Windows Wi-Fi 网卡。")
        _update_setup_state(
            state="connecting_setup",
            step="1/4 连接机器人配网热点",
            message=f"正在连接 {setup_ssid}…",
            original_ssid=original.get("ssid", ""),
            current_ssid=original.get("ssid", ""),
        )
        connected = windows_setup.connect_setup_hotspot(setup_ssid, original["name"])
        setup_connected = True
        _update_setup_state(
            state="sending_credentials",
            step="2/4 写入场地 Wi-Fi",
            message="已连接机器人；正在安全地把本次输入写入 ESP32 本地闪存…",
            current_ssid=connected.get("ssid", setup_ssid),
        )
        _robot_online.clear()
        windows_setup.send_wifi_credentials(target_ssid, password)
        time.sleep(2)

        _update_setup_state(
            state="restoring_network",
            step="3/4 恢复电脑原网络",
            message=f"机器人正在重启；正在恢复 {original.get('ssid') or '电脑原网络状态'}…",
        )
        restored_wifi = _restore_after_setup(original, setup_ssid)
        restored = True
        setup_connected = False
        _update_setup_state(
            state="waiting_online",
            step="4/4 等待 MQTT 和机器人上线",
            message="电脑网络已恢复；正在等待云端 MQTT 与机器人重新上线…",
            current_ssid=restored_wifi.get("ssid", ""),
        )

        deadline = time.monotonic() + 120
        internet = False
        while time.monotonic() < deadline:
            internet = windows_setup.internet_available(timeout=2)
            if internet and _mqtt_connected.is_set() and _robot_online.is_set():
                _update_setup_state(
                    state="success",
                    step="连接完成",
                    message="电脑网络、MQTT 和机器人均已恢复；无需重新烧录。",
                    error="",
                    completed_at=_now_iso(),
                )
                print(f"[设备向导] {setup_ssid} 已配置到 {target_ssid}，机器人重新上线。")
                return
            time.sleep(2)

        if not internet:
            reason = "电脑恢复原网络后仍无法访问互联网。"
        elif not _mqtt_connected.is_set():
            reason = "电脑可联网，但 MQTT 端口 1883 不可用；校园网或防火墙可能限制了该端口。"
        else:
            reason = "MQTT 已连接，但机器人未上线；请确认场地 Wi-Fi 提供 2.4 GHz 且密码正确。"
        raise windows_setup.SetupError(reason)
    except Exception as exc:
        recovery_error = ""
        if original and not restored:
            try:
                _restore_after_setup(original, setup_ssid)
                restored = True
            except Exception as restore_exc:
                recovery_error = f"；电脑原网络自动恢复失败：{restore_exc}"
        if setup_connected and original:
            windows_setup.remove_setup_profile(setup_ssid, original.get("name", ""))
        _update_setup_state(
            state="error",
            step="连接失败",
            message="",
            error=f"{exc}{recovery_error}",
            completed_at=_now_iso(),
        )
        print(f"[设备向导失败] {exc}{recovery_error}")


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/health", methods=["GET"])
def health():
    recording_status = experiment_recorder.status()
    microphone_status = current_microphone_status()
    with _cycle_lock:
        robot_busy = _robot_busy
    return jsonify(
        {
            "status": "ok",
            "session_id": _session_id,
            "mqtt_connected": _mqtt_connected.is_set(),
            "robot_online": _robot_online.is_set(),
            "robot_busy": robot_busy,
            "microphones": microphone_status["online_nodes"],
            "audio_capture": microphone_status,
            "experiment_state": recording_status["state"],
            "default_output_dir": str(experiment_recorder.default_output_dir),
        }
    )


# 机器人端可显示的表情名（与固件 parseExpressionName / README 一致）。
_VALID_EXPRESSIONS = {"focus", "stable", "reminder", "curious"}


@app.route("/api/expr", methods=["POST"])
def send_expression_api():
    """手动下发表情指令给机器人，格式 expr:<name>。

    请求体: {"expression": "focus|stable|reminder|curious"}
    用于在网页控制台手动切换表情（测试/调试用）。只改变 TFT 显示，不移动机器人，
    也不改变云端的干预统计。机器人若离线，可正常返回但下发失败会提示。
    """
    body = request.get_json(silent=True) or {}
    name = str(body.get("expression") or "").strip().lower()
    if name not in _VALID_EXPRESSIONS:
        return jsonify(
            {
                "status": "error",
                "message": f"无效表情：{name or '(空)'}。可选：focus / stable / reminder / curious",
            }
        ), 400

    client = _mqtt_client_ref
    published = False
    if client is not None and client.is_connected():
        client.publish(MQTT_TOPIC_CONTROL, f"expr:{name}")
        published = True

    result = {
        "status": "ok" if published else "offline",
        "expression": name,
        "published": published,
        "message": f"已下发 expr:{name}" if published else "MQTT 未连接或机器人不在线，指令未下发（仍可稍后重试）。",
        "mqtt_connected": bool(_mqtt_connected.is_set()),
        "robot_online": bool(_robot_online.is_set()),
    }
    return jsonify(result), 200 if published else 502


@app.route("/api/expression/upload", methods=["POST"])
def upload_expression_api():
    """上传一张 480x320 1-bit 表情 PNG，自动转成固件 .h 并替换所选槽位。

    请求: multipart/form-data，字段 file=<png> 和 slot=<focus|reminder|curious|stable>。
    覆盖位置: robot_esp32/1.3/<slot 对应 .h>（focus 额外保留 #define IMG_W/IMG_H）。
    复用 robot_esp32/1.3/png_to_h.py 的转码逻辑。
    """
    import sys
    import tempfile
    from pathlib import Path as _P

    # 定位固件目录：源码运行在 BASE_DIR.parent/robot_esp32/1.3；冻结(exe)时先看打包进
    # _internal/firmware/png_to_h.py（若固件目录不存在则退化为只读回显）。
    frozen = getattr(sys, "frozen", False)
    candidates = []
    if not frozen:
        candidates.append(_P(BASE_DIR).parent / "robot_esp32" / "1.3")
    firmware_dir = None
    for cand in candidates:
        if (cand / "stable_image.h").is_file():
            firmware_dir = cand
            break

    upload = request.files.get("file")
    slot = str(request.form.get("slot") or "").strip().lower()
    if upload is None or not upload.filename:
        return jsonify({"status": "error", "message": "未收到图片文件。"}), 400

    # 复用 png_to_h 的转码逻辑（含 SLOTS、png_to_bytes、bytes_to_h、尺寸校验）。
    tool_dirs = []
    if firmware_dir is not None:
        tool_dirs.append(str(firmware_dir))
    bundled = _P(getattr(sys, "_MEIPASS", _P(BASE_DIR))) / "firmware"
    if (bundled / "png_to_h.py").is_file():
        tool_dirs.append(str(bundled))
    for tool_dir in tool_dirs:
        if tool_dir not in sys.path:
            sys.path.insert(0, tool_dir)
    try:
        import png_to_h
    except SystemExit:
        # png_to_h.py 缺 Pillow 时会直接 SystemExit，需转成可读错误而非断连。
        return jsonify(
            {"status": "error", "message": "缺少 Pillow 库，无法转码。请先安装：python -m pip install pillow"}
        ), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": f"加载转码模块失败：{exc}"}), 500

    if slot not in png_to_h.SLOTS:
        return jsonify(
            {
                "status": "error",
                "message": f"无效槽位：{slot}。可选：{' / '.join(sorted(png_to_h.SLOTS))}",
            }
        ), 400

    filename, array_name = png_to_h.SLOTS[slot]

    # 固件目录存在则写进对应槽位；否则落到本机 cloud_brain/data/expressions/ 供手动拷贝。
    if firmware_dir is not None:
        out_dir = firmware_dir
    else:
        out_dir = _P(BASE_DIR) / "data" / "expressions"
        out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    # 存临时 png 再交给 png_to_bytes（它从磁盘读取并校验 480x320）。
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(upload.read())
            tmp_path = _P(tmp.name)
        data = png_to_h.png_to_bytes(str(tmp_path))
        content = png_to_h.bytes_to_h(data, array_name, upload.filename or f"{slot}.png", slot)
        tmp_path.unlink(missing_ok=True)
    except ValueError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except (NameError, UnboundLocalError):
            pass
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except (NameError, UnboundLocalError):
            pass
        return jsonify({"status": "error", "message": f"转码失败：{exc}"}), 500

    try:
        out_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return jsonify(
            {"status": "error", "message": f"写入 {out_path.name} 失败：{exc}"}
        ), 500

    print(f"[表情] 已上传并转码 {slot} -> {out_path.name}（{len(data)} 字节）")
    return jsonify(
        {
            "status": "ok",
            "slot": slot,
            "file": out_path.name,
            "array_name": array_name,
            "bytes": len(data),
            "message": f"已生成 {out_path.name}（数组 {array_name}，{len(data)} 字节）。编译固件后生效。",
        }
    )


@app.route("/api/experiment/start", methods=["POST"])
@serialize_experiment_api
def start_experiment_api():
    if _no_mic_mode:
        return jsonify(
            {"status": "error", "message": "--no-mic 模式不能开始正式录音。"}
        ), 409
    microphones = refresh_microphones(force=True)
    if sorted(microphones) != ["node1", "node2", "node3", "node4"]:
        scan_detail = f" 设备扫描失败：{_microphone_scan_error}" if _microphone_scan_error else ""
        return jsonify(
            {
                "status": "error",
                "message": (
                    f"只检测到 {len(microphones)} 路麦克风，必须先接齐并命名 4 路设备。"
                    f"{scan_detail}"
                ),
            }
        ), 409

    body = request.get_json(silent=True) or {}
    # require_robot=false => 机器人未连接也能开始录音（仅测麦克风）；默认为 True（仍要求机器人）。
    global _no_robot_mode
    _no_robot_mode = not bool(body.get("require_robot", True))

    if not _no_robot_mode and (not _mqtt_connected.is_set() or not _robot_online.is_set()):
        return jsonify(
            {"status": "error", "message": "云端 MQTT 或机器人尚未在线，拒绝开始实验。"}
        ), 409

    with _cycle_lock:
        if not _no_robot_mode and _robot_busy:
            return jsonify(
                {"status": "busy", "message": "机器人仍在执行任务，暂不能开始新实验。"}
            ), 409

    output_dir = str(body.get("output_dir") or "").strip()
    group_number = body.get("group_number")
    if isinstance(group_number, str):
        group_number = group_number.strip()

    try:
        clear_audio_analysis_queues()
        recording = experiment_recorder.start(
            output_dir=output_dir or None,
            group_number=group_number,
            nodes=sorted(microphones),
        )
        reset_runtime_state(
            archive_current=False,
            reason="explicit_experiment_start",
            session_id=recording["experiment_id"],
            started_at=recording["started_at"],
        )
        _experiment_ending.clear()
        _experiment_active.set()
        _brain_flush_request.clear()
        _brain_flushed.clear()
        start_audio_capture()
    except (RecordingError, OSError, RuntimeError, sd.PortAudioError) as exc:
        _experiment_active.clear()
        cleanup_error = None
        try:
            stop_audio_capture()
        except RuntimeError as stop_exc:
            cleanup_error = stop_exc
        clear_audio_analysis_queues()
        if experiment_recorder.status()["state"] in {
            "recording",
            "finalizing",
            "stopped",
            "error",
        }:
            try:
                experiment_recorder.abort_failed_start()
            except RecordingError as abort_exc:
                cleanup_error = abort_exc
        if cleanup_error is not None and experiment_recorder.status()["state"] == "ready":
            experiment_recorder.mark_error(
                f"实验启动失败后的音频流清理不完整：{cleanup_error}。请重启后台。"
            )
        if cleanup_error is None:
            message = f"实验未能开始：{exc}。已回到就绪状态，可以修复设备后重试。"
        else:
            message = (
                f"实验未能开始：{exc}。清理未完成：{cleanup_error}。"
                "请保留页面显示的暂存目录并重启后台。"
            )
        return jsonify(
            {
                "status": "error",
                "message": message,
                "recording": experiment_recorder.status(),
            }
        ), 500

    print(
        f"[EXPERIMENT START] {recording['experiment_id']} -> "
        f"{recording['output_dir']}"
    )
    return jsonify(
        {
            "status": "success",
            "message": "实验已明确开始，4 路录音正在写入本地暂存区。",
            "recording": experiment_recorder.status(),
        }
    ), 201


@app.route("/api/experiment/end", methods=["POST"])
@serialize_experiment_api
def end_experiment_api():
    global _pending_session_snapshot

    if experiment_recorder.status()["state"] != "recording":
        return jsonify(
            {"status": "error", "message": "当前没有正在进行的实验，不能重复结束。"}
        ), 409

    # 先禁止新的调度，但保留音频分析线程运行，直到四路队列和最后一句都处理完成。
    _experiment_ending.set()
    try:
        stream_stop_error = None
        try:
            stop_audio_capture()
        except RuntimeError as exc:
            stream_stop_error = exc
        experiment_recorder.stop()
        if stream_stop_error is not None:
            print(f"[EXPERIMENT STREAM WARNING] {stream_stop_error}")
        flush_audio_processing(timeout=30)
        _experiment_active.clear()

        snapshot = snapshot_runtime_state("explicit_experiment_end")
        if stream_stop_error is not None:
            snapshot["audio_stream_stop_warning"] = str(stream_stop_error)
        snapshot["recording"] = experiment_recorder.status()
        _pending_session_snapshot = snapshot
        exported = experiment_recorder.export(snapshot)
    except (RecordingError, OSError, RuntimeError) as exc:
        _experiment_active.clear()
        print(f"[EXPERIMENT EXPORT ERROR] {exc}")
        recording_status = experiment_recorder.status()
        retry_available = recording_status["state"] in {"stopped", "export_failed"}
        if retry_available:
            if _pending_session_snapshot is None:
                _pending_session_snapshot = snapshot_runtime_state(
                    "explicit_experiment_end_deferred"
                )
            _pending_session_snapshot["finalization_error"] = str(exc)
            try:
                recording_status = experiment_recorder.defer_export(exc)
            except RecordingError:
                retry_available = False
                recording_status = experiment_recorder.status()
        if retry_available:
            recovery = "本地暂存文件已保留，后台不会退出；修复问题后点击“重试传送并校验”。"
        else:
            recovery = (
                "录音写入或 WAV 封口完整性检查失败，后台不会退出。"
                "暂存数据已保留，但系统不会把它标记为完整录音；请记录暂存目录并人工检查。"
            )
        return jsonify(
            {
                "status": "error",
                "message": f"实验录音已经停止，但收尾未完成：{exc}。{recovery}",
                "retry_available": retry_available,
                "recording": recording_status,
            }
        ), 500

    client = _mqtt_client_ref
    if client is not None and client.is_connected():
        client.publish(MQTT_TOPIC_CONTROL, "expr:focus")

    print(
        f"[EXPERIMENT END] {exported['experiment_id']} -> "
        f"{exported['destination_dir']}"
    )
    success_message = "实验已明确结束，录音已传送并校验；后台即将自动关闭。"
    if exported.get("staging_cleanup_warning"):
        success_message += exported["staging_cleanup_warning"]
    schedule_backend_shutdown()
    return jsonify(
        {
            "status": "success",
            "message": success_message,
            "export": exported,
            "recording": experiment_recorder.status(),
            "backend_shutdown_in_seconds": 3,
        }
    )


@app.route("/api/experiment/retry-export", methods=["POST"])
@serialize_experiment_api
def retry_experiment_export_api():
    if _pending_session_snapshot is None:
        return jsonify(
            {"status": "error", "message": "当前没有可重试传送的实验数据。"}
        ), 409
    try:
        retry_snapshot = snapshot_runtime_state("explicit_experiment_retry_export")
        retry_snapshot["previous_finalization"] = _pending_session_snapshot
        retry_snapshot["recording"] = experiment_recorder.status()
        exported = experiment_recorder.export(retry_snapshot)
    except (RecordingError, OSError) as exc:
        return jsonify(
            {
                "status": "error",
                "message": f"重试传送失败：{exc}",
                "recording": experiment_recorder.status(),
            }
        ), 500

    success_message = "重试传送及校验成功，后台即将自动关闭。"
    if exported.get("staging_cleanup_warning"):
        success_message += exported["staging_cleanup_warning"]
    schedule_backend_shutdown()
    return jsonify(
        {
            "status": "success",
            "message": success_message,
            "export": exported,
            "backend_shutdown_in_seconds": 3,
        }
    )


@app.route("/api/session/reset", methods=["POST"])
@serialize_experiment_api
def reset_session_api():
    if _experiment_active.is_set():
        return jsonify(
            {"status": "error", "message": "实验正在录音，必须使用明确的“结束实验”。"}
        ), 409
    try:
        result = reset_runtime_state(
            archive_current=True,
            reason="operator_started_next_group",
            require_idle=True,
        )
    except RuntimeError as exc:
        return jsonify({"status": "busy", "message": str(exc)}), 409
    except OSError as exc:
        return jsonify(
            {
                "status": "error",
                "message": f"归档写入失败，当前数据未清空：{exc}",
            }
        ), 500

    return jsonify(
        {
            "status": "success",
            "message": "上一组已归档，当前统计和干预次数已清零。",
            **result,
        }
    )


@app.route("/api/get_meeting_data", methods=["GET"])
def get_meeting_data():
    microphone_status = current_microphone_status()
    with _cycle_lock:
        robot_busy = _robot_busy
        task_started_at = _robot_task_started_at
        intervention_counts = dict(_intervention_counts)
        next_check_at = _next_schedule_check_at
        with state_lock:
            times = dict(speaking_times)
            total = sum(times.values())
            response = {
                "status": "success",
                "session_id": _session_id,
                "session_started_at": _session_started_at,
                "last_archive_path": _last_archive_path,
                "current_speaking_times": times,
                "total_speaking_time": total,
                "latest_records": meeting_records[-10:],
                "latest_speaking_events": list(speaking_events)[-10:],
                "latest_audio_state": dict(latest_audio_state),
                "intervention_counts": intervention_counts,
                "robot_busy": robot_busy,
                "robot_task_elapsed_seconds": (
                    max(0.0, time.monotonic() - task_started_at)
                    if robot_busy and task_started_at
                    else 0.0
                ),
                "mqtt_connected": _mqtt_connected.is_set(),
                "robot_online": _robot_online.is_set(),
                "next_schedule_check_at": next_check_at,
                "microphones": microphone_status["online_nodes"],
                "audio_capture": microphone_status,
                "experiment": experiment_recorder.status(),
                "experiment_active": _experiment_active.is_set(),
                "experiment_ending": _experiment_ending.is_set(),
                "default_output_dir": str(experiment_recorder.default_output_dir),
            }
    return jsonify(response)


def get_whisper_model():
    # 延迟加载 Whisper，避免导入模块时就下载/加载模型，方便排查启动问题。
    # faster-whisper 与 Silero VAD 都是可选能力，只在真正需要时加载。
    global _whisper_model
    if _whisper_model is None:
        print("[启动] 正在加载 Faster-Whisper tiny 模型，请稍等...")
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print("[启动] Faster-Whisper 模型加载完成。")
    return _whisper_model


def get_decibels(audio_bytes):
    # 将 PCM int16 音频转换成分贝，用于判断当前哪个麦克风最活跃。
    arr = np.frombuffer(audio_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(arr.astype(np.float32) ** 2))
    return 20 * np.log10(rms + 1e-6)


def calibrate_noise_floor():
    print(f"[校准] 请保持现场安静 {CALIBRATION_SECONDS:.0f} 秒，正在测量各麦克风底噪...")
    samples = {node: [] for node in audio_queues}
    target_count = max(1, int(CALIBRATION_SECONDS / CHUNK_DURATION))

    while any(len(values) < target_count for values in samples.values()):
        if _brain_flush_request.is_set() and (
            _experiment_ending.is_set() or not _experiment_active.is_set()
        ):
            break
        chunks = dequeue_aligned_audio_chunks()
        if chunks is not None:
            for node, audio_bytes in chunks.items():
                if len(samples[node]) < target_count:
                    samples[node].append(get_decibels(audio_bytes))
        else:
            time.sleep(0.01)

    noise_floor = {
        node: float(np.median(values)) if values else -80.0
        for node, values in samples.items()
    }
    floor_text = " ".join(f"{node}={db:.1f}dB" for node, db in sorted(noise_floor.items()))
    print(f"[校准] 底噪测量完成：{floor_text}")
    return noise_floor


def save_to_wav(audio_frames, filename):
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(audio_frames))


def dequeue_aligned_audio_chunks():
    """Atomically take one analysis block from every microphone, or take none."""
    with _audio_queue_lock:
        if not all(not audio_queue.empty() for audio_queue in audio_queues.values()):
            return None
        return {node: audio_queue.get_nowait() for node, audio_queue in audio_queues.items()}


def clear_audio_analysis_queues():
    """Remove analysis-only blocks while no microphone streams are running."""
    with _audio_queue_lock:
        for audio_queue in audio_queues.values():
            while True:
                try:
                    audio_queue.get_nowait()
                except queue.Empty:
                    break


def queue_transcription(session_generation, node_name, frames, max_db):
    # whisper 关闭时没有 whisper_worker 消费 transcribe_queue；若仍入队，
    # unfinished_tasks 永不归零，结束实验时 flush_audio_processing 会误判为
    # "语音转写未在限定时间内完成" 而失败。此处直接返回，不产生任何待转写任务。
    if not USE_WHISPER:
        return
    transcribe_queue.put(
        (session_generation, node_name, list(frames), float(max_db))
    )


def find_renamed_microphones():
    # 扫描系统录音设备，只接入命名为 NODE*_MIC 的 4 个麦克风。
    target_mics = {}
    for i, dev in enumerate(sd.query_devices()):
        dev_name = dev["name"].upper()
        for expected in NODE_HARDWARE_MAP:
            if expected in dev_name and dev["max_input_channels"] > 0:
                hostapi = sd.query_hostapis(dev["hostapi"])["name"]
                if "MME" in hostapi or "DirectSound" in hostapi:
                    node_key = expected.split("_")[0].lower()
                    if node_key not in target_mics:
                        target_mics[node_key] = i
    return target_mics


def refresh_microphones(force=False):
    """Refresh the idle/start-time device map without probing active streams."""
    global _microphones, _last_microphone_scan_monotonic, _microphone_scan_error

    now = time.monotonic()
    with _microphone_lock:
        if (
            not force
            and _last_microphone_scan_monotonic
            and now - _last_microphone_scan_monotonic < MICROPHONE_SCAN_INTERVAL_SECONDS
        ):
            return dict(_microphones)
        try:
            found = find_renamed_microphones()
        except (OSError, RuntimeError, sd.PortAudioError) as exc:
            _microphones = {}
            _microphone_scan_error = str(exc)
            _last_microphone_scan_monotonic = now
            return {}
        _microphones = found
        _microphone_scan_error = ""
        _last_microphone_scan_monotonic = now
        return dict(_microphones)


def current_microphone_status():
    """Return physical devices while idle and callback-liveness while recording."""
    expected = ["node1", "node2", "node3", "node4"]
    if not _experiment_active.is_set():
        devices = refresh_microphones()
        online = sorted(devices)
        return {
            "online_nodes": online,
            "missing_nodes": [node for node in expected if node not in devices],
            "last_chunk_age_seconds": {},
            "callback_errors": {},
            "scan_error": _microphone_scan_error,
        }

    now = time.monotonic()
    with _audio_health_lock:
        last_chunks = dict(_audio_last_chunk_monotonic)
        callback_errors = dict(_audio_callback_errors)
    ages = {
        node: round(max(0.0, now - timestamp), 3)
        for node, timestamp in last_chunks.items()
    }
    online = sorted(
        node
        for node in expected
        if node in ages and ages[node] <= AUDIO_CALLBACK_TIMEOUT_SECONDS
    )
    return {
        "online_nodes": online,
        "missing_nodes": [node for node in expected if node not in online],
        "last_chunk_age_seconds": ages,
        "callback_errors": callback_errors,
        "scan_error": "",
    }


@app.route("/api/setup/status", methods=["GET"])
def setup_status_api():
    connectivity = _connectivity_snapshot()
    microphone_status = current_microphone_status()
    return jsonify(
        {
            "status": "success",
            "platform_supported": sys.platform.startswith("win"),
            "computer": {
                "internet_available": connectivity["internet_available"],
                "wifi_ssid": connectivity["wifi"].get("ssid", ""),
                "wifi_band": connectivity["wifi"].get("band", ""),
                "wifi_interface": connectivity["wifi"].get("name", ""),
                "error": connectivity["error"],
            },
            "mqtt_connected": _mqtt_connected.is_set(),
            "robot_online": _robot_online.is_set(),
            "microphones": microphone_status["online_nodes"],
            "provisioning": _setup_state_snapshot(),
        }
    )


@app.route("/api/setup/networks", methods=["GET"])
def setup_networks_api():
    if not sys.platform.startswith("win"):
        return jsonify({"status": "error", "message": "设备连接向导仅支持 Windows。"}), 409
    if _setup_state_snapshot()["state"] in {
        "connecting_setup", "sending_credentials", "restoring_network", "waiting_online"
    }:
        return jsonify({"status": "busy", "message": "设备连接流程正在进行，暂不能重新扫描。"}), 409
    try:
        networks = windows_setup.visible_networks()
        connection = windows_setup.connected_wifi()
    except windows_setup.SetupError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500
    return jsonify(
        {
            "status": "success",
            "networks": networks,
            "setup_hotspots": [
                item for item in networks if item["ssid"].startswith("EffMeet-Setup-")
            ],
            "current_wifi": connection,
        }
    )


@app.route("/api/setup/provision", methods=["POST"])
def setup_provision_api():
    global _setup_thread
    if not sys.platform.startswith("win"):
        return jsonify({"status": "error", "message": "设备连接向导仅支持 Windows。"}), 409
    if _experiment_active.is_set():
        return jsonify({"status": "error", "message": "实验正在录音，禁止切换电脑网络。"}), 409

    body = request.get_json(silent=True) or {}
    setup_ssid = str(body.get("setup_ssid") or "").strip()
    target_ssid = str(body.get("target_ssid") or "").strip()
    password = str(body.get("password") or "")
    if not setup_ssid.startswith("EffMeet-Setup-") or len(setup_ssid) > 32:
        return jsonify({"status": "error", "message": "请选择有效的 EffMeet-Setup-XXXX 热点。"}), 400
    if not target_ssid or len(target_ssid) > 32 or len(password) > 63:
        return jsonify({"status": "error", "message": "场地 Wi-Fi 名称或密码长度无效。"}), 400

    try:
        networks = windows_setup.visible_networks()
    except windows_setup.SetupError as exc:
        return jsonify({"status": "error", "message": f"扫描场地 Wi-Fi 失败：{exc}"}), 500
    target_error = windows_setup.validate_target_network(networks, target_ssid)
    if target_error:
        return jsonify({"status": "error", "message": target_error}), 409

    with _setup_state_lock:
        if _setup_state["state"] in {
            "connecting_setup", "sending_credentials", "restoring_network", "waiting_online"
        }:
            return jsonify({"status": "busy", "message": "已有设备连接流程正在进行。"}), 409
        _setup_state.update(
            state="starting",
            step="准备连接",
            message="正在保存电脑当前网络状态…",
            error="",
            setup_ssid=setup_ssid,
            target_ssid=target_ssid,
            original_ssid="",
            current_ssid="",
            started_at=_now_iso(),
            completed_at="",
        )
    _setup_thread = threading.Thread(
        target=_provision_robot_wifi,
        args=(setup_ssid, target_ssid, password),
        name="effmeet-device-setup",
        daemon=True,
    )
    _setup_thread.start()
    return jsonify(
        {
            "status": "accepted",
            "message": "设备连接流程已开始；请保持本页面打开，电脑网络会短暂切换后自动恢复。",
            "provisioning": _setup_state_snapshot(),
        }
    ), 202


def start_audio_capture():
    global _audio_streams, _audio_workers_started
    global _audio_last_chunk_monotonic, _audio_callback_errors

    with _stream_lock:
        if _audio_streams:
            raise RuntimeError("音频输入流已经启动，拒绝重复开始。")

        streams = []
        with _audio_health_lock:
            _audio_last_chunk_monotonic = {}
            _audio_callback_errors = {}
        try:
            for node_name, device_index in sorted(_microphones.items()):
                def cb(indata, frames, time_info, status, name=node_name):
                    try:
                        if status:
                            print(f"[麦克风状态] {name}: {status}")
                        audio_bytes = indata.copy().tobytes()
                        captured_at_ns = time.time_ns()
                        experiment_recorder.capture(name, audio_bytes, captured_at_ns)
                        audio_queues[name].put(audio_bytes)
                        with _audio_health_lock:
                            _audio_last_chunk_monotonic[name] = time.monotonic()
                            _audio_callback_errors.pop(name, None)
                    except Exception as exc:
                        with _audio_health_lock:
                            _audio_callback_errors[name] = str(exc)
                        print(f"[麦克风回调错误] {name}: {exc}")

                stream = sd.InputStream(
                    device=device_index,
                    channels=1,
                    samplerate=SAMPLE_RATE,
                    dtype="int16",
                    blocksize=int(SAMPLE_RATE * CHUNK_DURATION),
                    callback=cb,
                )
                streams.append((node_name, device_index, stream))

            # 先构造全部输入流，只有四路都能打开后才依次开始采集。
            for node_name, device_index, stream in streams:
                stream.start()
                print(f"[麦克风] {node_name} 正式开始录音，设备编号={device_index}")

            deadline = time.monotonic() + AUDIO_START_TIMEOUT_SECONDS
            expected = {"node1", "node2", "node3", "node4"}
            while time.monotonic() < deadline:
                with _audio_health_lock:
                    started_nodes = set(_audio_last_chunk_monotonic)
                    callback_errors = dict(_audio_callback_errors)
                if started_nodes == expected:
                    break
                if callback_errors:
                    raise RuntimeError(
                        "麦克风回调启动失败："
                        + "；".join(f"{node}={error}" for node, error in sorted(callback_errors.items()))
                    )
                time.sleep(0.05)
            else:
                missing = "、".join(sorted(expected - started_nodes))
                raise RuntimeError(f"麦克风未在 {AUDIO_START_TIMEOUT_SECONDS:.0f} 秒内送回音频：{missing}")

            _audio_streams = [stream for _node, _device, stream in streams]
            if not _audio_workers_started:
                threading.Thread(target=brain_worker, daemon=True).start()
                _audio_workers_started = True
        except Exception:
            for _node, _device, stream in streams:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            _audio_streams = []
            raise


def stop_audio_capture():
    global _audio_streams

    with _stream_lock:
        streams = list(_audio_streams)
        _audio_streams = []

    errors = []
    for stream in streams:
        try:
            stream.stop()
        except Exception as exc:
            errors.append(str(exc))
        try:
            stream.close()
        except Exception as exc:
            errors.append(str(exc))
    if streams:
        print("[麦克风] 四路输入流已明确停止。")
    if errors:
        raise RuntimeError("停止音频流时发生错误：" + "；".join(errors))


def flush_audio_processing(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _audio_queue_lock:
            queue_sizes = {
                node: audio_queue.qsize() for node, audio_queue in audio_queues.items()
            }
            if all(size == 0 for size in queue_sizes.values()):
                discarded = None
                finished = True
            elif any(size == 0 for size in queue_sizes.values()):
                # 四路流已经停止后，数量可能相差最后一个块。无法做跨麦克风比较的
                # 尾块只从分析队列移除；原始 PCM 已独立写入四个 WAV，不会丢失。
                discarded = {}
                for node, audio_queue in audio_queues.items():
                    count = 0
                    while True:
                        try:
                            audio_queue.get_nowait()
                            count += 1
                        except queue.Empty:
                            break
                    if count:
                        discarded[node] = count
                finished = True
            else:
                discarded = None
                finished = False
            if discarded:
                print(
                    f"[音频收尾] 丢弃无法跨通道对齐的分析尾块：{discarded}；"
                    "四路原始 WAV 不受影响。"
                )
            if finished:
                break
        time.sleep(0.05)
    else:
        raise RuntimeError("音频分析队列未能在限定时间内清空。")

    _brain_flushed.clear()
    _brain_flush_request.set()
    remaining = max(0.1, deadline - time.monotonic())
    if not _brain_flushed.wait(remaining):
        raise RuntimeError("最后一句语音未能在限定时间内送入转写队列。")

    while time.monotonic() < deadline:
        if transcribe_queue.unfinished_tasks == 0:
            return
        time.sleep(0.05)
    raise RuntimeError("语音转写未能在限定时间内完成。")


def schedule_backend_shutdown(delay_seconds=3):
    def request_shutdown():
        print("[SHUTDOWN] 实验已完整结束，后台现在自动关闭。")
        _shutdown_requested.set()

    timer = threading.Timer(delay_seconds, request_shutdown)
    timer.daemon = True
    timer.start()


def whisper_worker():
    # 后台转写线程：把收集到的一段语音保存成临时 wav，再交给 Whisper 转文字。
    global USE_WHISPER
    os.makedirs("temp_audio", exist_ok=True)
    try:
        model = get_whisper_model()
    except Exception as exc:
        # 可选依赖、模型缓存或网络不可用时，保留核心录音/判定/干预能力。
        USE_WHISPER = False
        while True:
            try:
                transcribe_queue.get_nowait()
                transcribe_queue.task_done()
            except queue.Empty:
                break
        print(f"[转写] 未启用：{exc}。核心录音、发言判定和 MQTT 干预仍继续。")
        return

    while True:
        session_generation, node_name, frames, max_db = transcribe_queue.get()
        temp_file = f"temp_audio/{node_name}_{time.time_ns()}.wav"
        try:
            save_to_wav(frames, temp_file)
            segments, _info = model.transcribe(temp_file, beam_size=5)
            text = "".join(seg.text for seg in segments).strip()
            if text:
                with state_lock:
                    if session_generation != _session_generation:
                        print(f"[转写] 丢弃上一实验组的延迟结果：{node_name}")
                    else:
                        print(f"[转写] {node_name}: {text}")
                        meeting_records.append(
                            {
                                "node": node_name,
                                "time": time.strftime("%H:%M:%S"),
                                "text": text,
                                "decibel": round(float(max_db), 1),
                            }
                        )
        except Exception as exc:
            print(f"[转写] 当前片段失败，已跳过：{exc}")
        finally:
            try:
                os.remove(temp_file)
            except OSError:
                pass
            transcribe_queue.task_done()


def _send_next_intervention(client):
    # 同时下发目标座位与本次到达后需要展示的表情。
    global _robot_task_started_at
    target, expression = _active_interventions[_cycle_index]
    payload = f"move:{target}:{expression}"
    publish_info = client.publish(MQTT_TOPIC_CONTROL, payload)
    if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
        print(f"[发送失败] MQTT rc={publish_info.rc}，未下发：{payload}")
        return False

    _robot_task_started_at = time.monotonic()
    print(
        f"[发送] -> {MQTT_TOPIC_CONTROL}: '{payload}' "
        f"（{_cycle_index + 1}/{len(_active_interventions)}）"
    )
    sys.stdout.flush()
    return True


def _parse_status_fields(payload):
    fields = {}
    for part in payload.split("|")[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _on_robot_done(client, msg_payload):
    # 收到机器人 done 消息后推进调度序列；一轮结束后广播 cycle_done。
    global _robot_busy, _cycle_index, _robot_task_started_at
    print(f"[接收] <- {MQTT_TOPIC_STATUS}: '{msg_payload}'")

    with _cycle_lock:
        if not _robot_busy or not _active_interventions:
            print("[忽略] 当前没有在途任务，这是一条过期的 done。")
            return

        expected_target, expected_expression = _active_interventions[_cycle_index]
        fields = _parse_status_fields(msg_payload)
        if fields.get("target") and fields["target"] != str(expected_target):
            print(
                f"[忽略] done 目标不匹配：期待 {expected_target}，"
                f"收到 {fields['target']}。"
            )
            return
        if fields.get("expression") and fields["expression"] != expected_expression:
            print(
                f"[忽略] done 表情不匹配：期待 {expected_expression}，"
                f"收到 {fields['expression']}。"
            )
            return

        _cycle_index += 1
        if _cycle_index >= len(_active_interventions):
            _cycle_index = 0
            _robot_busy = False
            _robot_task_started_at = 0.0
            _active_interventions.clear()
            client.publish(MQTT_TOPIC_CYCLE_DONE, "cycle_done")
            print(f"[完成] 本轮干预结束，已发布 {MQTT_TOPIC_CYCLE_DONE}。")
        else:
            print("[下一步] 机器人已完成，继续发送下一个干预目标。")
            if not _send_next_intervention(client):
                _robot_busy = False
                _robot_task_started_at = 0.0
                _active_interventions.clear()

    sys.stdout.flush()


def _on_robot_error(msg_payload):
    global _robot_busy, _cycle_index, _robot_task_started_at
    print(f"[机器人故障] <- {MQTT_TOPIC_STATUS}: '{msg_payload}'")
    fields = _parse_status_fields(msg_payload)

    with _cycle_lock:
        if not _robot_busy or not _active_interventions:
            return
        expected_target, _expression = _active_interventions[_cycle_index]
        if fields.get("target") and fields["target"] != str(expected_target):
            return

        node = f"node{expected_target}"
        if _intervention_counts[node] > 0:
            _intervention_counts[node] -= 1
        _robot_busy = False
        _robot_task_started_at = 0.0
        _cycle_index = 0
        _active_interventions.clear()

    sys.stdout.flush()


def robot_watchdog_worker():
    """Release a lost in-flight task promptly instead of waiting for the next schedule."""
    global _robot_busy, _cycle_index, _robot_task_started_at

    while True:
        time.sleep(1)
        timed_out = False
        with _cycle_lock:
            if not _robot_busy or not _robot_task_started_at or not _active_interventions:
                continue
            elapsed = time.monotonic() - _robot_task_started_at
            if elapsed < ROBOT_TASK_TIMEOUT_SECONDS:
                continue

            target = _active_interventions[_cycle_index][0]
            node = f"node{target}"
            if _intervention_counts[node] > 0:
                _intervention_counts[node] -= 1
            _robot_busy = False
            _robot_task_started_at = 0.0
            _cycle_index = 0
            _active_interventions.clear()
            timed_out = True

        if timed_out:
            client = _mqtt_client_ref
            if client is not None and client.is_connected():
                client.publish(MQTT_TOPIC_CONTROL, "expr:focus")
            print(
                f"[超时恢复] 机器人任务超过 {ROBOT_TASK_TIMEOUT_SECONDS}s，"
                "已解除忙状态；不会再要求人工杀后台。"
            )
            sys.stdout.flush()


def mqtt_monitor_worker(schedule_interval):
    global _robot_busy, _cycle_index, _mqtt_client_ref
    global _robot_task_started_at, _next_schedule_check_at

    client_id = "EffMeet_Brain_" + str(random.randint(10000, 99999))
    client = mqtt.Client(client_id=client_id)
    print(f"[MQTT] 客户端 ID: {client_id}")
    _mqtt_client_ref = client

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            _mqtt_connected.set()
            c.subscribe(MQTT_TOPIC_STATUS)
            c.publish(MQTT_TOPIC_CONTROL, "expr:focus")
            print(f"[MQTT] 已连接 Broker，并订阅机器人状态主题：{MQTT_TOPIC_STATUS}")
            print("[MQTT] 已下发等待表情：expr:focus")
        else:
            _mqtt_connected.clear()
            print(f"[MQTT] 连接异常，返回码：{rc}")
        sys.stdout.flush()

    def on_disconnect(c, userdata, rc):
        _mqtt_connected.clear()
        print(f"[MQTT] 连接已断开 rc={rc}，后台将自动重连。")
        sys.stdout.flush()

    def on_message(c, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if payload == "online":
            _robot_online.set()
            print("[机器人] 设备在线。")
        elif payload == "offline":
            _robot_online.clear()
            print("[机器人] 设备离线。")
        elif payload.startswith("done"):
            _robot_online.set()
            _on_robot_done(c, payload)
        elif payload.startswith("error"):
            _robot_online.set()
            _on_robot_error(payload)
        elif payload.startswith("ack"):
            _robot_online.set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    try:
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print("[MQTT] 机器人调度线程已上线，将持续自动重连。")
    except Exception as e:
        print(f"[MQTT] 连接失败：{e}")
        return

    while True:
        _next_schedule_check_at = time.time() + schedule_interval
        if _scheduler_reset_event.wait(schedule_interval):
            _scheduler_reset_event.clear()
            print("[调度] 已开始新实验组，检查倒计时重新计算。")
            continue

        if not _experiment_active.is_set() or _experiment_ending.is_set():
            print("[调度] 实验未在采集中或正在结束，本轮不结算。")
            continue

        if not _no_robot_mode and (not _mqtt_connected.is_set() or not _robot_online.is_set()):
            print("[调度] MQTT 或机器人尚未在线，本轮不结算，避免生成无法执行的任务。")
            continue

        with _cycle_lock:
            if not _no_robot_mode and _robot_busy:
                print("[调度] 机器人正在执行任务，等待下一轮检查。")
                continue

            with state_lock:
                total = sum(speaking_times.values())
                sorted_nodes = sorted(speaking_times.items(), key=lambda x: x[1])

            if total <= 5:
                client.publish(MQTT_TOPIC_CONTROL, "expr:stable")
                print(
                    f"[调度] 总发言时长 {total:.1f}s，不触发干预；"
                    "已发送稳定表情，4 秒后机器人自动恢复专注。"
                )
                continue

            avg_time = total / 4.0
            threshold = avg_time * IMBALANCE_RATIO_THRESHOLD

            candidate_node, candidate_time = sorted_nodes[0]

            if candidate_time < threshold:
                target_num = int(candidate_node.replace("node", ""))
                _intervention_counts[candidate_node] += 1
                intervention_count = _intervention_counts[candidate_node]
                expression = "reminder" if intervention_count == 1 else "curious"
                _active_interventions[:] = [(target_num, expression)]
                _cycle_index = 0
                _robot_busy = True
                print(
                    f"[触发] {candidate_node} 发言 {candidate_time:.1f}s，"
                    f"低于阈值 {threshold:.1f}s；第 {intervention_count} 次干预，"
                    f"表情={expression}"
                )
                print(f"[触发] 本轮干预：{_active_interventions}")
                if not _send_next_intervention(client):
                    _intervention_counts[candidate_node] -= 1
                    _robot_busy = False
                    _robot_task_started_at = 0.0
                    _active_interventions.clear()
            else:
                client.publish(MQTT_TOPIC_CONTROL, "expr:stable")
                print(
                    f"[调度] 发言分布暂时均衡。候选节点 {candidate_node}: "
                    f"{candidate_time:.1f}s >= {threshold:.1f}s；"
                    "已发送稳定表情，4 秒后机器人自动恢复专注。"
                )

        sys.stdout.flush()

def brain_worker():
    # 后台音频分析线程：持续读取 4 路麦克风，统计发言时长并切分可转写片段。
    vad_engine = None
    if USE_VAD:
        try:
            vad_engine = VADEngine(sample_rate=SAMPLE_RATE)
        except Exception as e:
            print(f"[VAD] 初始化失败，将临时退回到纯分贝判断：{e}")
    current_speaker = None
    audio_buffer = []
    silence_ticks = 0
    max_db_in_sentence = 0
    last_debug_print = 0
    noise_floor = calibrate_noise_floor()
    if _activity_engine is not None:
        _activity_engine.set_floor(noise_floor)
    with state_lock:
        worker_session_generation = _session_generation

    print("[大脑] 音频监听与分析线程已启动。")
    while True:
        if _brain_flush_request.is_set():
            if current_speaker and len(audio_buffer) > 1:
                queue_transcription(
                    worker_session_generation,
                    current_speaker,
                    audio_buffer,
                    max_db_in_sentence,
                )
            current_speaker = None
            audio_buffer = []
            silence_ticks = 0
            max_db_in_sentence = 0
            _brain_flush_request.clear()
            _brain_flushed.set()
            time.sleep(0.01)
            continue

        if not _experiment_active.is_set():
            time.sleep(0.01)
            continue

        with state_lock:
            active_session_generation = _session_generation
        if worker_session_generation != active_session_generation:
            current_speaker = None
            audio_buffer = []
            silence_ticks = 0
            max_db_in_sentence = 0
            worker_session_generation = active_session_generation

        chunks = dequeue_aligned_audio_chunks()
        if chunks is not None:
            db_values = {n: get_decibels(chunks[n]) for n in audio_queues}

            # robust 模式：自适应底噪 + 主导说话人 + 静音容忍。
            # 只复用最紧凑的输出（dom / count），转写片段与上报沿用下方通用逻辑。
            if _activity_engine is not None and DETECT_MODE == "robust":
                res = _activity_engine.update(db_values)
                winner_node = res["dom"]
                is_speaking = res["count"]
                decision = "计时：" + (winner_node or "?") + f" +{CHUNK_DURATION:.1f}s" \
                    if is_speaking else \
                    ("未计时：整场静音" if not res["vad_active"] else "未计时：主导者未确认")
                max_db = db_values.get(winner_node, -80.0) if winner_node else -80.0
                max_score = res["snr"].get(winner_node, 0.0) if winner_node else 0.0
                runner_up_node = None
                runner_up_score = 0.0
                winner_gap = 0.0
                with state_lock:
                    if worker_session_generation != _session_generation:
                        continue
                    latest_audio_state.clear()
                    latest_audio_state.update(
                        {
                            "time": time.strftime("%H:%M:%S"),
                            "mode": "robust",
                            "candidate": winner_node,
                            "dom": winner_node,
                            "vad_active": bool(res["vad_active"]),
                            "is_speaking": bool(is_speaking),
                            "decision": decision,
                            "db_values": {n: round(float(v), 1) for n, v in db_values.items()},
                            "snr": dict(res["snr"]),
                            "noise_floor": dict(res["floor"]),
                        }
                    )
                if is_speaking:
                    with state_lock:
                        if worker_session_generation != _session_generation:
                            continue
                        speaking_times[winner_node] += CHUNK_DURATION
                        speaking_events.append(
                            {
                                "time": time.strftime("%H:%M:%S"),
                                "node": winner_node,
                                "add_seconds": CHUNK_DURATION,
                                "total_seconds": round(float(speaking_times[winner_node]), 1),
                                "mode": "robust",
                            }
                        )
                    if current_speaker != winner_node:
                        if current_speaker and len(audio_buffer) > 1:
                            queue_transcription(
                                worker_session_generation,
                                current_speaker,
                                audio_buffer,
                                max_db_in_sentence,
                            )
                        current_speaker = winner_node
                        audio_buffer = [chunks[winner_node]]
                        max_db_in_sentence = max_db
                    else:
                        audio_buffer.append(chunks[winner_node])
                        max_db_in_sentence = max(max_db_in_sentence, max_db)
                    silence_ticks = 0
                else:
                    silence_ticks += 1
                    if silence_ticks > 3 and current_speaker is not None:
                        if len(audio_buffer) > 1:
                            queue_transcription(
                                worker_session_generation,
                                current_speaker,
                                audio_buffer,
                                max_db_in_sentence,
                            )
                        current_speaker = None
                        audio_buffer = []
            else:
                # legacy / 引擎不可用：走旧的瞬时判定。
                score_values = {
                    n: db_values[n] - noise_floor[n] + MIC_GAIN_OFFSETS_DB.get(n, 0.0)
                    for n in audio_queues
                }
                ranked_nodes = sorted(score_values, key=score_values.get, reverse=True)
                winner_node = ranked_nodes[0]
                runner_up_node = ranked_nodes[1] if len(ranked_nodes) > 1 else winner_node
                max_db = db_values[winner_node]
                max_score = score_values[winner_node]
                runner_up_score = score_values[runner_up_node]
                winner_gap = max_score - runner_up_score

                # 每隔 2 秒打印一次分贝，便于现场校准麦克风阈值。
                now = time.time()
                if now - last_debug_print >= 2:
                    db_text = " ".join(f"{n}={db_values[n]:.1f}dB" for n in sorted(db_values))
                    score_text = " ".join(f"{n}={score_values[n]:+.1f}" for n in sorted(score_values))
                    print(
                        f"[分贝] {db_text} | 相对分={score_text} | "
                        f"候选={winner_node} 绝对={max_db:.1f}dB 相对={max_score:.1f}dB "
                        f"领先={winner_gap:.1f}dB"
                    )
                    last_debug_print = now

                # VAD 可用时叠加人声判断；VAD 初始化失败时退回到纯分贝判断。
                vad_passed = True if vad_engine is None else vad_engine.is_speech(chunks[winner_node])
                is_speaking = (
                    max_db > ABSOLUTE_DB_FLOOR
                    and max_score > SPEECH_ABOVE_NOISE_DB
                    and winner_gap >= WINNER_MARGIN_DB
                    and vad_passed
                )

                if not is_speaking:
                    if max_db <= ABSOLUTE_DB_FLOOR:
                        decision = f"未计时：绝对分贝 {max_db:.1f}dB 低于 {ABSOLUTE_DB_FLOOR:.1f}dB"
                    elif max_score <= SPEECH_ABOVE_NOISE_DB:
                        decision = f"未计时：相对底噪 {max_score:.1f}dB 低于 {SPEECH_ABOVE_NOISE_DB:.1f}dB"
                    elif winner_gap < WINNER_MARGIN_DB:
                        decision = f"未计时：领先差 {winner_gap:.1f}dB 小于 {WINNER_MARGIN_DB:.1f}dB，疑似串音"
                    elif not vad_passed:
                        decision = "未计时：VAD 未判定为人声"
                    else:
                        decision = "未计时"
                else:
                    decision = f"计时：{winner_node} +{CHUNK_DURATION:.1f}s"

                with state_lock:
                    if worker_session_generation != _session_generation:
                        continue
                    latest_audio_state.clear()
                    latest_audio_state.update(
                        {
                            "time": time.strftime("%H:%M:%S"),
                            "mode": "legacy",
                            "candidate": winner_node,
                            "runner_up": runner_up_node,
                            "candidate_db": round(float(max_db), 1),
                            "candidate_score": round(float(max_score), 1),
                            "runner_up_score": round(float(runner_up_score), 1),
                            "winner_gap": round(float(winner_gap), 1),
                            "vad_passed": bool(vad_passed),
                            "is_speaking": bool(is_speaking),
                            "decision": decision,
                            "db_values": {n: round(float(v), 1) for n, v in db_values.items()},
                            "score_values": {n: round(float(v), 1) for n, v in score_values.items()},
                            "noise_floor": {n: round(float(v), 1) for n, v in noise_floor.items()},
                        }
                    )

                if is_speaking:
                    with state_lock:
                        if worker_session_generation != _session_generation:
                            continue
                        speaking_times[winner_node] += CHUNK_DURATION
                        speaking_events.append(
                            {
                                "time": time.strftime("%H:%M:%S"),
                                "node": winner_node,
                                "add_seconds": CHUNK_DURATION,
                                "total_seconds": round(float(speaking_times[winner_node]), 1),
                                "candidate_db": round(float(max_db), 1),
                                "candidate_score": round(float(max_score), 1),
                                "winner_gap": round(float(winner_gap), 1),
                            }
                        )

                    # 说话人变化时，把上一位说话人的缓存片段送去转写。
                    if current_speaker != winner_node:
                        if current_speaker and len(audio_buffer) > 1:
                            queue_transcription(
                                worker_session_generation,
                                current_speaker,
                                audio_buffer,
                                max_db_in_sentence,
                            )
                        current_speaker = winner_node
                        audio_buffer = [chunks[winner_node]]
                        max_db_in_sentence = max_db
                    else:
                        audio_buffer.append(chunks[winner_node])
                        max_db_in_sentence = max(max_db_in_sentence, max_db)
                    silence_ticks = 0
                else:
                    silence_ticks += 1
                    if silence_ticks > 3 and current_speaker is not None:
                        # 连续静音后认为一句话结束，将缓存音频交给转写线程。
                        if len(audio_buffer) > 1:
                            queue_transcription(
                                worker_session_generation,
                                current_speaker,
                                audio_buffer,
                                max_db_in_sentence,
                            )
                        current_speaker = None
                        audio_buffer = []
        else:
            time.sleep(0.01)


def main():
    # 程序入口：后台只做就绪准备；明确点击“开始实验”后才打开麦克风录音。
    global _microphones, _no_mic_mode, DETECT_MODE, _activity_engine
    global USE_WHISPER, USE_VAD
    args = parse_args(sys.argv[1:])
    if args.schedule_interval > 0:
        global SILENCE_TIMEOUT
        SILENCE_TIMEOUT = args.schedule_interval

    # 默认开启转写与 VAD；打 exe 剥离 torch 时，用 --no-whisper / --no-vad 关闭。
    # PyInstaller 打包（sys.frozen）默认关闭，保证 exe 双击即可运行、不依赖 torch。
    USE_WHISPER = not getattr(sys, "frozen", False)
    USE_VAD = not getattr(sys, "frozen", False)
    if args.no_whisper:
        USE_WHISPER = False
        print("[启动] 已禁用语音转写（--no-whisper），仅录音+人声判定+干预。")
    if args.no_vad:
        USE_VAD = False
        print("[启动] 已禁用 Silero VAD（--no-vad），robust 人声判定仍生效。")

    DETECT_MODE = args.detect_mode
    if DETECT_MODE == "robust":
        _activity_engine = ActivityEngine(
            nodes=sorted({f"node{i}" for i in range(1, 5)}),
            speech_hi_db=SPEECH_HI_DB,
            speech_lo_db=SPEECH_LO_DB,
            floor_alpha=FLOOR_ALPHA,
            max_sil=VAD_MAX_SIL,
            hangover=DOM_HANGOVER,
            lead_confirm=DOM_LEAD_CONFIRM,
        )
        print(f"[启动] 说话人判定模式：robust（自适应底噪 + 主导说话人 + 静音容忍）")
    else:
        print(f"[启动] 说话人判定模式：legacy（旧瞬时判定，可回退）")

    if args.loose_thresholds:
        global ABSOLUTE_DB_FLOOR, SPEECH_ABOVE_NOISE_DB, WINNER_MARGIN_DB
        ABSOLUTE_DB_FLOOR = 38.0
        SPEECH_ABOVE_NOISE_DB = 8.0
        WINNER_MARGIN_DB = 3.0
        print(
            f"[THRESHOLD] loose mode enabled: "
            f"ABS={ABSOLUTE_DB_FLOOR:.1f}dB, SCORE={SPEECH_ABOVE_NOISE_DB:.1f}dB, GAP={WINNER_MARGIN_DB:.1f}dB"
        )

    reset_runtime_state()
    _microphones = refresh_microphones(force=True)
    _no_mic_mode = bool(args.no_mic)
    print(f"[启动] 已检测到麦克风节点：{list(_microphones.keys())}")

    if len(_microphones) < 4 and not args.no_mic:
        print(f"[未就绪] 只找到 {len(_microphones)} 个麦克风，需要 4 个。")
        print("请在 Windows 录音设备中把 4 个麦克风分别重命名为：")
        print("NODE1_MIC / NODE2_MIC / NODE3_MIC / NODE4_MIC")

    if args.smoke:
        if len(_microphones) == 4:
            print("[SMOKE] 麦克风检查通过，跳过 Web / MQTT / 音频线程启动。")
        else:
            print("[SMOKE] 麦克风检查未通过。")
        return

    if args.no_mic:
        print("[NO-MIC] 跳过正式录音能力，保留 Web / MQTT 用于接口联调。")
        if args.demo_state:
            try:
                apply_demo_state(args.demo_state)
            except Exception as e:
                print(f"[DEMO] 发言时长注入失败：{e}")
                return

    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    if USE_WHISPER:
        threading.Thread(target=whisper_worker, daemon=True).start()
    threading.Thread(target=mqtt_monitor_worker, args=(SILENCE_TIMEOUT,), daemon=True).start()
    threading.Thread(target=robot_watchdog_worker, daemon=True).start()

    print("\n[READY] 后台已就绪，但实验尚未开始，当前不会录音。")
    print("        请在实验控制台明确点击“开始实验”。")
    print(f"        默认录音目标：{experiment_recorder.default_output_dir}")
    print(f"        调度器每 {SILENCE_TIMEOUT}s 检查一次发言分布。")
    print(
        f"        当前阈值：ABS={ABSOLUTE_DB_FLOOR:.1f}dB, "
        f"SCORE={SPEECH_ABOVE_NOISE_DB:.1f}dB, GAP={WINNER_MARGIN_DB:.1f}dB"
    )
    print("        按 Ctrl+C 退出。\n")
    sys.stdout.flush()

    try:
        while not _shutdown_requested.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n[退出] 收到人工停止信号。")
    finally:
        _experiment_active.clear()
        _experiment_ending.set()
        try:
            stop_audio_capture()
        except RuntimeError as exc:
            print(f"[退出警告] {exc}")
        if experiment_recorder.status()["state"] == "recording":
            try:
                experiment_recorder.stop()
                print(
                    "[退出保护] 录音已封口并保留在暂存区，但未传送；"
                    "请勿删除 staging_dir。"
                )
            except RecordingError as exc:
                print(f"[退出错误] {exc}")

        client = _mqtt_client_ref
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        print("[退出] EffMeet 后台已关闭。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
