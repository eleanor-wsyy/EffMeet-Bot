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
from faster_whisper import WhisperModel

from core.vad_engine import VADEngine
from experiment_recording import ExperimentRecorder, RecordingError


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
CALIBRATION_SECONDS = 3.0
ABSOLUTE_DB_FLOOR = 45.0
SPEECH_ABOVE_NOISE_DB = 10.0
WINNER_MARGIN_DB = 4.0
USE_VAD = True

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
_audio_workers_started = False
_no_mic_mode = False
_pending_session_snapshot = None
_experiment_api_lock = threading.Lock()


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


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/health", methods=["GET"])
def health():
    recording_status = experiment_recorder.status()
    with _cycle_lock:
        robot_busy = _robot_busy
    return jsonify(
        {
            "status": "ok",
            "session_id": _session_id,
            "mqtt_connected": _mqtt_connected.is_set(),
            "robot_online": _robot_online.is_set(),
            "robot_busy": robot_busy,
            "microphones": sorted(_microphones),
            "experiment_state": recording_status["state"],
            "default_output_dir": str(experiment_recorder.default_output_dir),
        }
    )


@app.route("/api/experiment/start", methods=["POST"])
@serialize_experiment_api
def start_experiment_api():
    if _no_mic_mode:
        return jsonify(
            {"status": "error", "message": "--no-mic 模式不能开始正式录音。"}
        ), 409
    if sorted(_microphones) != ["node1", "node2", "node3", "node4"]:
        return jsonify(
            {
                "status": "error",
                "message": f"只检测到 {len(_microphones)} 路麦克风，必须先接齐并命名 4 路设备。",
            }
        ), 409
    if not _mqtt_connected.is_set() or not _robot_online.is_set():
        return jsonify(
            {"status": "error", "message": "云端 MQTT 或机器人尚未在线，拒绝开始实验。"}
        ), 409

    with _cycle_lock:
        if _robot_busy:
            return jsonify(
                {"status": "busy", "message": "机器人仍在执行任务，暂不能开始新实验。"}
            ), 409

    body = request.get_json(silent=True) or {}
    output_dir = str(body.get("output_dir") or "").strip()
    group_number = body.get("group_number")
    if isinstance(group_number, str):
        group_number = group_number.strip()

    try:
        clear_audio_analysis_queues()
        recording = experiment_recorder.start(
            output_dir=output_dir or None,
            group_number=group_number,
            nodes=sorted(_microphones),
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
                "microphones": sorted(_microphones),
                "experiment": experiment_recorder.status(),
                "experiment_active": _experiment_active.is_set(),
                "experiment_ending": _experiment_ending.is_set(),
                "default_output_dir": str(experiment_recorder.default_output_dir),
            }
    return jsonify(response)


def get_whisper_model():
    # 延迟加载 Whisper，避免导入模块时就下载/加载模型，方便排查启动问题。
    global _whisper_model
    if _whisper_model is None:
        print("[启动] 正在加载 Faster-Whisper tiny 模型，请稍等...")
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


def start_audio_capture():
    global _audio_streams, _audio_workers_started

    with _stream_lock:
        if _audio_streams:
            raise RuntimeError("音频输入流已经启动，拒绝重复开始。")

        streams = []
        try:
            for node_name, device_index in sorted(_microphones.items()):
                def cb(indata, frames, time_info, status, name=node_name):
                    if status:
                        print(f"[麦克风状态] {name}: {status}")
                    audio_bytes = indata.copy().tobytes()
                    captured_at_ns = time.time_ns()
                    experiment_recorder.capture(name, audio_bytes, captured_at_ns)
                    audio_queues[name].put(audio_bytes)

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
    os.makedirs("temp_audio", exist_ok=True)
    model = get_whisper_model()

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

        if not _mqtt_connected.is_set() or not _robot_online.is_set():
            print("[调度] MQTT 或机器人尚未在线，本轮不结算，避免生成无法执行的任务。")
            continue

        with _cycle_lock:
            if _robot_busy:
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
    global _microphones, _no_mic_mode
    args = parse_args(sys.argv[1:])
    if args.schedule_interval > 0:
        global SILENCE_TIMEOUT
        SILENCE_TIMEOUT = args.schedule_interval

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
    _microphones = find_renamed_microphones()
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
