# -*- coding: utf-8 -*-
"""Precise four-channel experiment recording and verified export."""

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path


class RecordingError(RuntimeError):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def timestamp_ns_to_iso(timestamp_ns):
    if not timestamp_ns:
        return None
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).astimezone().isoformat(
        timespec="milliseconds"
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ExperimentRecorder:
    """Write all microphone blocks locally, then atomically export and verify them."""

    _STOP = object()
    _MAX_MISSING_AUDIO_SECONDS = 2.0
    _DIGITAL_SILENCE_MIN_SECONDS = 10.0

    def __init__(self, staging_root, sample_rate=16000, channels=1, sample_width=2):
        self.staging_root = Path(staging_root).resolve()
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_width = int(sample_width)
        self.default_output_dir = (Path.home() / "Documents" / "EffMeet_Recordings").resolve()

        self._lock = threading.Lock()
        self._state = "ready"
        self._accepting = False
        self._queue = None
        self._writer_thread = None
        self._writers = {}
        self._writer_error = None
        self._dropped_chunks = 0
        self._frames = {}
        self._first_chunk_wall_ns = {}
        self._last_chunk_wall_ns = {}
        self._has_nonzero_pcm = {}
        self._nodes = []

        self.experiment_id = None
        self.group_number = None
        self.output_root = None
        self.staging_dir = None
        self.destination_dir = None
        self.started_at = None
        self.ended_at = None
        self.started_monotonic_ns = None
        self.ended_monotonic_ns = None
        self.last_error = None

    def _verify_output_root(self, output_dir):
        output_root = Path(output_dir).expanduser()
        if not output_root.is_absolute():
            output_root = output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        probe = output_root / f".effmeet-write-test-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_bytes(b"EffMeet")
            if probe.read_bytes() != b"EffMeet":
                raise RecordingError(f"目标路径写入校验失败：{output_root}")
        finally:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
        return output_root.resolve()

    @staticmethod
    def _next_group_number(output_root, date_text):
        pattern = re.compile(rf"^{re.escape(date_text)}_\d{{6}}_group(\d+)$")
        groups = []
        for item in output_root.iterdir():
            if not item.is_dir():
                continue
            match = pattern.match(item.name)
            if match:
                groups.append(int(match.group(1)))
        return max(groups, default=0) + 1

    def start(self, output_dir=None, group_number=None, nodes=None):
        nodes = list(nodes or ["node1", "node2", "node3", "node4"])
        if sorted(nodes) != ["node1", "node2", "node3", "node4"]:
            raise RecordingError("录音必须且只能包含 node1、node2、node3、node4。")

        with self._lock:
            if self._state != "ready":
                raise RecordingError(f"当前录音状态不允许开始：{self._state}")

        output_root = self._verify_output_root(output_dir or self.default_output_dir)
        started_wall = datetime.now().astimezone()
        date_text = started_wall.strftime("%Y%m%d")
        if group_number in (None, ""):
            group_number = self._next_group_number(output_root, date_text)
        try:
            group_number = int(group_number)
        except (TypeError, ValueError) as exc:
            raise RecordingError("组号必须是正整数，留空则按当天已有实验自动递增。") from exc
        if not 1 <= group_number <= 9999:
            raise RecordingError("组号必须在 1 到 9999 之间。")

        experiment_id = (
            f"{started_wall:%Y%m%d_%H%M%S}_group{group_number:03d}"
        )
        destination_dir = output_root / experiment_id
        if destination_dir.exists():
            raise RecordingError(f"目标实验目录已存在，请更换组号：{destination_dir}")

        self.staging_root.mkdir(parents=True, exist_ok=True)
        staging_dir = self.staging_root / f".{experiment_id}-{uuid.uuid4().hex}.recording"
        staging_dir.mkdir(parents=False, exist_ok=False)

        writers = {}
        try:
            for node in nodes:
                path = staging_dir / f"{experiment_id}_{node}.wav"
                writer = wave.open(str(path), "wb")
                writer.setnchannels(self.channels)
                writer.setsampwidth(self.sample_width)
                writer.setframerate(self.sample_rate)
                writers[node] = writer
        except Exception:
            for writer in writers.values():
                writer.close()
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        with self._lock:
            self._state = "recording"
            self._accepting = True
            # 不设容量上限：宁可短时占用内存，也不能静默丢失实验音频块。
            self._queue = queue.Queue()
            self._writers = writers
            self._writer_error = None
            self._dropped_chunks = 0
            self._frames = {node: 0 for node in nodes}
            self._first_chunk_wall_ns = {node: None for node in nodes}
            self._last_chunk_wall_ns = {node: None for node in nodes}
            self._has_nonzero_pcm = {node: False for node in nodes}
            self._nodes = nodes

            self.experiment_id = experiment_id
            self.group_number = group_number
            self.output_root = output_root
            self.staging_dir = staging_dir
            self.destination_dir = None
            self.started_at = started_wall.isoformat(timespec="milliseconds")
            self.ended_at = None
            self.started_monotonic_ns = time.monotonic_ns()
            self.ended_monotonic_ns = None
            self.last_error = None

            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name=f"recording-writer-{experiment_id}",
                daemon=True,
            )
            self._writer_thread.start()

        return self.status()

    def capture(self, node, audio_bytes, captured_wall_ns=None):
        captured_wall_ns = captured_wall_ns or time.time_ns()
        item = (node, bytes(audio_bytes), captured_wall_ns)
        with self._lock:
            if not self._accepting or self._state != "recording":
                return False
            if node not in self._frames:
                self._dropped_chunks += 1
                self.last_error = f"收到未知录音节点：{node}"
                return False
            if len(item[1]) % (self.channels * self.sample_width) != 0:
                self._dropped_chunks += 1
                self.last_error = f"{node} 收到不完整 PCM 帧。"
                return False
            if any(item[1]):
                self._has_nonzero_pcm[node] = True
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped_chunks += 1
                self.last_error = "录音写入队列溢出，存在音频块丢失。"
                return False
        return True

    def _writer_loop(self):
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    node, audio_bytes, captured_wall_ns = item
                    writer = self._writers[node]
                    writer.writeframesraw(audio_bytes)
                    frames = len(audio_bytes) // (self.channels * self.sample_width)
                    self._frames[node] += frames
                    if self._first_chunk_wall_ns[node] is None:
                        self._first_chunk_wall_ns[node] = captured_wall_ns
                    self._last_chunk_wall_ns[node] = captured_wall_ns
                except Exception as exc:
                    with self._lock:
                        if self._writer_error is None:
                            self._writer_error = exc
                            self.last_error = f"录音写入失败：{exc}"
                finally:
                    self._queue.task_done()
        finally:
            for writer in self._writers.values():
                try:
                    writer.close()
                except Exception as exc:
                    with self._lock:
                        if self._writer_error is None:
                            self._writer_error = exc
                            self.last_error = f"WAV 封口失败：{exc}"

    def stop(self, timeout=30):
        with self._lock:
            if self._state != "recording":
                raise RecordingError(f"当前没有正在进行的录音：{self._state}")
            self._accepting = False
            self.ended_at = now_iso()
            self.ended_monotonic_ns = time.monotonic_ns()
            self._state = "finalizing"
            self._queue.put_nowait(self._STOP)
            writer_thread = self._writer_thread

        writer_thread.join(timeout=timeout)
        if writer_thread.is_alive():
            with self._lock:
                self._state = "error"
                self.last_error = "WAV 文件在限定时间内未能完成封口。"
            raise RecordingError(self.last_error)

        with self._lock:
            if self._writer_error is not None:
                self._state = "error"
                raise RecordingError(self.last_error)
            if self._dropped_chunks:
                self._state = "error"
                self.last_error = f"检测到 {self._dropped_chunks} 个音频块丢失，拒绝标记为完整录音。"
                raise RecordingError(self.last_error)
            integrity_error = self._audio_integrity_error_locked()
            if integrity_error:
                self._state = "error"
                self.last_error = integrity_error
                raise RecordingError(self.last_error)
            self._state = "stopped"
        return self.status()

    def _audio_integrity_error_locked(self):
        durations = {
            node: self._frames[node] / self.sample_rate
            for node in self._nodes
        }
        empty_nodes = [node for node, frames in self._frames.items() if frames <= 0]
        if empty_nodes:
            return "以下麦克风没有录到任何音频：" + "、".join(sorted(empty_nodes))

        shortest = min(durations.values())
        longest = max(durations.values())
        if longest - shortest > self._MAX_MISSING_AUDIO_SECONDS:
            detail = "，".join(
                f"{node}={seconds:.1f}s" for node, seconds in sorted(durations.items())
            )
            return f"四路录音时长不一致，疑似中途掉线：{detail}"

        elapsed = 0.0
        if self.started_monotonic_ns and self.ended_monotonic_ns:
            elapsed = (self.ended_monotonic_ns - self.started_monotonic_ns) / 1_000_000_000
        if elapsed - shortest > self._MAX_MISSING_AUDIO_SECONDS:
            return (
                f"四路录音均早于实验结束停止：实验 {elapsed:.1f}s，"
                f"最短录音 {shortest:.1f}s"
            )

        silent_nodes = [
            node
            for node, seconds in durations.items()
            if seconds >= self._DIGITAL_SILENCE_MIN_SECONDS
            and not self._has_nonzero_pcm[node]
        ]
        if silent_nodes:
            return "以下麦克风长时间只有数字静音：" + "、".join(sorted(silent_nodes))
        return None

    def abort_failed_start(self, timeout=30):
        """Discard a session that never reached a successful explicit start response."""
        nothing_to_abort = False
        with self._lock:
            if self._state == "recording":
                self._accepting = False
                self.ended_at = now_iso()
                self.ended_monotonic_ns = time.monotonic_ns()
                self._state = "finalizing"
                self._queue.put_nowait(self._STOP)
            elif self._state not in {"finalizing", "stopped", "error"}:
                nothing_to_abort = True
            writer_thread = self._writer_thread

        if nothing_to_abort:
            return self.status()

        if writer_thread is not None and writer_thread.is_alive():
            writer_thread.join(timeout=timeout)
        if writer_thread is not None and writer_thread.is_alive():
            with self._lock:
                self._state = "error"
                self.last_error = (
                    "实验启动失败后，WAV 写入线程未能停止；暂存数据已保留。"
                )
            raise RecordingError(self.last_error)

        staging_dir = self.staging_dir
        if staging_dir is not None and staging_dir.exists():
            resolved_staging = staging_dir.resolve()
            try:
                resolved_staging.relative_to(self.staging_root)
            except ValueError as exc:
                with self._lock:
                    self._state = "error"
                    self.last_error = "拒绝清理录音暂存根目录之外的路径。"
                raise RecordingError(self.last_error) from exc
            try:
                shutil.rmtree(resolved_staging)
            except OSError as exc:
                with self._lock:
                    self._state = "error"
                    self.last_error = f"启动失败的暂存目录无法清理：{exc}"
                raise RecordingError(self.last_error) from exc

        with self._lock:
            self._state = "ready"
            self._accepting = False
            self._queue = None
            self._writer_thread = None
            self._writers = {}
            self._writer_error = None
            self._dropped_chunks = 0
            self._frames = {}
            self._first_chunk_wall_ns = {}
            self._last_chunk_wall_ns = {}
            self._has_nonzero_pcm = {}
            self._nodes = []
            self.experiment_id = None
            self.group_number = None
            self.output_root = None
            self.staging_dir = None
            self.destination_dir = None
            self.started_at = None
            self.ended_at = None
            self.started_monotonic_ns = None
            self.ended_monotonic_ns = None
            self.last_error = None
        return self.status()

    def defer_export(self, error):
        """Mark intact, closed staging data as waiting for a later export retry."""
        with self._lock:
            if self._state not in {"stopped", "export_failed"}:
                raise RecordingError(
                    f"当前录音状态不能进入传送重试：{self._state}"
                )
            self._state = "export_failed"
            self.last_error = str(error)
        return self.status()

    def mark_error(self, error):
        """Block further lifecycle actions after cleanup or integrity becomes uncertain."""
        with self._lock:
            self._accepting = False
            self._state = "error"
            self.last_error = str(error)
        return self.status()

    def _build_manifest(self, session_snapshot):
        files = []
        for path in sorted(self.staging_dir.iterdir()):
            if not path.is_file() or path.name.endswith("_manifest.json"):
                continue
            entry = {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for node in self._nodes:
                if path.name.endswith(f"_{node}.wav"):
                    entry.update(
                        {
                            "node": node,
                            "frames": self._frames[node],
                            "duration_seconds": round(
                                self._frames[node] / self.sample_rate, 6
                            ),
                            "first_chunk_at": timestamp_ns_to_iso(
                                self._first_chunk_wall_ns[node]
                            ),
                            "last_chunk_at": timestamp_ns_to_iso(
                                self._last_chunk_wall_ns[node]
                            ),
                        }
                    )
                    break
            files.append(entry)

        elapsed = None
        if self.started_monotonic_ns and self.ended_monotonic_ns:
            elapsed = round(
                (self.ended_monotonic_ns - self.started_monotonic_ns)
                / 1_000_000_000,
                6,
            )
        return {
            "schema_version": 1,
            "status": "complete",
            "experiment_id": self.experiment_id,
            "group_number": self.group_number,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "wall_clock_elapsed_seconds": elapsed,
            "audio_format": {
                "sample_rate_hz": self.sample_rate,
                "channels_per_file": self.channels,
                "sample_width_bytes": self.sample_width,
                "encoding": "PCM signed 16-bit little-endian",
            },
            "destination_root": str(self.output_root),
            "session_id": session_snapshot.get("session_id"),
            "files": files,
        }

    def export(self, session_snapshot):
        with self._lock:
            if self._state not in {"stopped", "export_failed"}:
                raise RecordingError(f"录音尚未完整封口，不能传送：{self._state}")
            self._state = "exporting"

        session_path = self.staging_dir / f"{self.experiment_id}_session.json"
        manifest_path = self.staging_dir / f"{self.experiment_id}_manifest.json"
        partial_dir = self.output_root / f".{self.experiment_id}-{uuid.uuid4().hex}.partial"
        destination_dir = self.output_root / self.experiment_id

        try:
            session_path.write_text(
                json.dumps(session_snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest = self._build_manifest(session_snapshot)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            partial_dir.mkdir(parents=False, exist_ok=False)
            source_files = sorted(path for path in self.staging_dir.iterdir() if path.is_file())
            for source in source_files:
                shutil.copy2(source, partial_dir / source.name)

            for source in source_files:
                destination = partial_dir / source.name
                if source.stat().st_size != destination.stat().st_size:
                    raise RecordingError(f"文件大小校验失败：{source.name}")
                if sha256_file(source) != sha256_file(destination):
                    raise RecordingError(f"SHA-256 校验失败：{source.name}")

            if destination_dir.exists():
                raise RecordingError(f"目标目录已存在，拒绝覆盖：{destination_dir}")
            os.replace(partial_dir, destination_dir)

            with self._lock:
                self._state = "exported"
                self.destination_dir = destination_dir
                self.last_error = None

            resolved_staging = self.staging_dir.resolve()
            resolved_staging.relative_to(self.staging_root)
            cleanup_warning = None
            try:
                shutil.rmtree(resolved_staging)
            except OSError as cleanup_exc:
                cleanup_warning = (
                    "最终目录已完整校验，但本机暂存副本未能删除："
                    f"{cleanup_exc}"
                )
                with self._lock:
                    self.last_error = cleanup_warning
            return {
                "experiment_id": self.experiment_id,
                "group_number": self.group_number,
                "destination_dir": str(destination_dir),
                "manifest_path": str(destination_dir / manifest_path.name),
                "files": [str(destination_dir / source.name) for source in source_files],
                "staging_cleanup_warning": cleanup_warning,
            }
        except Exception as exc:
            if partial_dir.exists() and partial_dir.parent.resolve() == self.output_root.resolve():
                shutil.rmtree(partial_dir, ignore_errors=True)
            with self._lock:
                self._state = "export_failed"
                self.last_error = str(exc)
            if isinstance(exc, RecordingError):
                raise
            raise RecordingError(f"录音传送失败：{exc}") from exc

    def status(self):
        with self._lock:
            elapsed = 0.0
            if self.started_monotonic_ns:
                end_ns = self.ended_monotonic_ns or time.monotonic_ns()
                elapsed = max(
                    0.0,
                    (end_ns - self.started_monotonic_ns) / 1_000_000_000,
                )
            return {
                "state": self._state,
                "experiment_id": self.experiment_id,
                "group_number": self.group_number,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "elapsed_seconds": round(elapsed, 3),
                "output_dir": str(self.output_root or self.default_output_dir),
                "destination_dir": (
                    str(self.destination_dir) if self.destination_dir else None
                ),
                "staging_dir": str(self.staging_dir) if self.staging_dir else None,
                "frames": dict(self._frames),
                "dropped_chunks": self._dropped_chunks,
                "error": self.last_error,
            }
