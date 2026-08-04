# -*- coding: utf-8 -*-
import hashlib
import json
import re
import shutil
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from experiment_recording import ExperimentRecorder


NODES = ["node1", "node2", "node3", "node4"]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_precise_recording_export():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        recorder = ExperimentRecorder(root / "staging", sample_rate=16000)
        started = recorder.start(
            output_dir=root / "destination",
            group_number=7,
            nodes=NODES,
        )
        assert re.fullmatch(r"\d{8}_\d{6}_group007", started["experiment_id"])

        frames_per_chunk = 1600
        chunks_per_node = 10
        audio = (b"\x34\x12" * frames_per_chunk)
        for _index in range(chunks_per_node):
            for node in NODES:
                assert recorder.capture(node, audio)

        stopped = recorder.stop()
        assert stopped["state"] == "stopped"
        assert all(
            frames == frames_per_chunk * chunks_per_node
            for frames in stopped["frames"].values()
        )

        snapshot = {
            "session_id": started["experiment_id"],
            "started_at": started["started_at"],
            "ended_at": stopped["ended_at"],
            "speaking_times": {node: 0.0 for node in NODES},
        }
        exported = recorder.export(snapshot)
        destination = Path(exported["destination_dir"])
        assert destination.is_dir()
        assert destination.name == started["experiment_id"]
        assert not Path(stopped["staging_dir"]).exists()
        assert not list(destination.parent.glob("*.partial"))

        for node in NODES:
            wav_path = destination / f"{started['experiment_id']}_{node}.wav"
            with wave.open(str(wav_path), "rb") as handle:
                assert handle.getnchannels() == 1
                assert handle.getsampwidth() == 2
                assert handle.getframerate() == 16000
                assert handle.getnframes() == frames_per_chunk * chunks_per_node

        manifest_path = destination / f"{started['experiment_id']}_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "complete"
        assert manifest["group_number"] == 7
        assert manifest["session_id"] == started["experiment_id"]
        assert len([item for item in manifest["files"] if item.get("node")]) == 4
        for item in manifest["files"]:
            assert item["sha256"] == digest(destination / item["name"])


def test_daily_group_auto_increment():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        output = root / "destination"
        output.mkdir()
        today = datetime.now().strftime("%Y%m%d")
        (output / f"{today}_010101_group003").mkdir()
        (output / f"{today}_010102_group011").mkdir()

        recorder = ExperimentRecorder(root / "staging")
        status = recorder.start(output_dir=output, nodes=NODES)
        assert status["group_number"] == 12
        assert status["experiment_id"].endswith("_group012")
        for node in NODES:
            recorder.capture(node, b"\x00\x00" * 16)
        recorder.stop()


def test_failed_start_can_return_to_ready():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        recorder = ExperimentRecorder(root / "staging")
        started = recorder.start(output_dir=root / "destination", nodes=NODES)
        staging_dir = Path(started["staging_dir"])
        recorder.capture("node1", b"\x00\x00" * 16)

        status = recorder.abort_failed_start()
        assert status["state"] == "ready"
        assert status["experiment_id"] is None
        assert not staging_dir.exists()

        restarted = recorder.start(
            output_dir=root / "destination", group_number=2, nodes=NODES
        )
        assert restarted["state"] == "recording"
        recorder.abort_failed_start()


def test_closed_recording_can_be_deferred_and_retried():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        recorder = ExperimentRecorder(root / "staging")
        started = recorder.start(output_dir=root / "destination", nodes=NODES)
        for node in NODES:
            recorder.capture(node, b"\x00\x00" * 16)
        recorder.stop()

        deferred = recorder.defer_export("temporary destination failure")
        assert deferred["state"] == "export_failed"
        assert "temporary destination failure" in deferred["error"]

        exported = recorder.export({"session_id": started["experiment_id"]})
        assert Path(exported["destination_dir"]).is_dir()
        assert recorder.status()["state"] == "exported"


def test_verified_export_survives_staging_cleanup_failure():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        recorder = ExperimentRecorder(root / "staging")
        started = recorder.start(output_dir=root / "destination", nodes=NODES)
        for node in NODES:
            recorder.capture(node, b"\x00\x00" * 16)
        recorder.stop()
        staging_dir = Path(started["staging_dir"]).resolve()
        real_rmtree = shutil.rmtree

        def fail_only_for_staging(path, *args, **kwargs):
            if Path(path).resolve() == staging_dir:
                raise OSError("simulated local cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        with patch("experiment_recording.shutil.rmtree", side_effect=fail_only_for_staging):
            exported = recorder.export({"session_id": started["experiment_id"]})

        assert Path(exported["destination_dir"]).is_dir()
        assert staging_dir.is_dir()
        assert "simulated local cleanup failure" in exported["staging_cleanup_warning"]
        assert recorder.status()["state"] == "exported"


if __name__ == "__main__":
    test_precise_recording_export()
    test_daily_group_auto_increment()
    test_failed_start_can_return_to_ready()
    test_closed_recording_can_be_deferred_and_retried()
    test_verified_export_survives_staging_cleanup_failure()
    print("Experiment recording/export tests passed.")
