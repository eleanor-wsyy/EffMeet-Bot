# -*- coding: utf-8 -*-
import tempfile
import threading
import wave
from pathlib import Path

import main_brain as brain
from experiment_recording import ExperimentRecorder


NODES = ["node1", "node2", "node3", "node4"]


def test_http_start_end_export_shutdown_lifecycle():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        original = {
            "recorder": brain.experiment_recorder,
            "microphones": brain._microphones,
            "no_mic": brain._no_mic_mode,
            "start_audio": brain.start_audio_capture,
            "stop_audio": brain.stop_audio_capture,
            "flush_audio": brain.flush_audio_processing,
            "schedule_shutdown": brain.schedule_backend_shutdown,
            "pending_snapshot": brain._pending_session_snapshot,
            "mqtt": brain._mqtt_connected.is_set(),
            "robot": brain._robot_online.is_set(),
        }
        shutdown_calls = []

        try:
            brain.experiment_recorder = ExperimentRecorder(root / "staging")
            brain._microphones = {node: index for index, node in enumerate(NODES)}
            brain._no_mic_mode = False
            brain._mqtt_connected.set()
            brain._robot_online.set()
            brain.start_audio_capture = lambda: None
            brain.stop_audio_capture = lambda: None
            def assert_flush_while_analysis_active(timeout=30):
                assert brain._experiment_active.is_set()
                assert brain._experiment_ending.is_set()

            brain.flush_audio_processing = assert_flush_while_analysis_active
            brain.schedule_backend_shutdown = lambda delay_seconds=3: shutdown_calls.append(
                delay_seconds
            )

            client = brain.app.test_client()
            start_response = client.post(
                "/api/experiment/start",
                json={"output_dir": str(root / "output"), "group_number": 2},
            )
            assert start_response.status_code == 201, start_response.get_json()
            started = start_response.get_json()["recording"]
            assert started["state"] == "recording"
            assert started["experiment_id"].endswith("_group002")
            assert brain._experiment_active.is_set()

            audio = b"\x12\x34" * 800
            for _index in range(4):
                for node in NODES:
                    assert brain.experiment_recorder.capture(node, audio)

            end_response = client.post("/api/experiment/end")
            assert end_response.status_code == 200, end_response.get_json()
            result = end_response.get_json()
            assert result["status"] == "success"
            assert shutdown_calls == [3]
            assert not brain._experiment_active.is_set()
            assert brain._experiment_ending.is_set()

            destination = Path(result["export"]["destination_dir"])
            assert destination.exists()
            for node in NODES:
                wav_path = destination / f"{started['experiment_id']}_{node}.wav"
                with wave.open(str(wav_path), "rb") as handle:
                    assert handle.getnframes() == 3200

            status = client.get("/api/get_meeting_data").get_json()
            assert status["experiment"]["state"] == "exported"
            assert status["experiment_active"] is False
        finally:
            brain.experiment_recorder = original["recorder"]
            brain._microphones = original["microphones"]
            brain._no_mic_mode = original["no_mic"]
            brain.start_audio_capture = original["start_audio"]
            brain.stop_audio_capture = original["stop_audio"]
            brain.flush_audio_processing = original["flush_audio"]
            brain.schedule_backend_shutdown = original["schedule_shutdown"]
            brain._pending_session_snapshot = original["pending_snapshot"]
            brain._experiment_active.clear()
            brain._experiment_ending.clear()
            if original["mqtt"]:
                brain._mqtt_connected.set()
            else:
                brain._mqtt_connected.clear()
            if original["robot"]:
                brain._robot_online.set()
            else:
                brain._robot_online.clear()


def test_http_failed_audio_start_is_retryable():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        original = {
            "recorder": brain.experiment_recorder,
            "microphones": brain._microphones,
            "no_mic": brain._no_mic_mode,
            "start_audio": brain.start_audio_capture,
            "stop_audio": brain.stop_audio_capture,
            "mqtt": brain._mqtt_connected.is_set(),
            "robot": brain._robot_online.is_set(),
        }

        try:
            brain.experiment_recorder = ExperimentRecorder(root / "staging")
            brain._microphones = {node: index for index, node in enumerate(NODES)}
            brain._no_mic_mode = False
            brain._mqtt_connected.set()
            brain._robot_online.set()
            brain.stop_audio_capture = lambda: None

            def fail_audio_start():
                brain.experiment_recorder.capture("node1", b"\x00\x00" * 16)
                raise RuntimeError("simulated stream start failure")

            brain.start_audio_capture = fail_audio_start
            client = brain.app.test_client()
            failed = client.post(
                "/api/experiment/start",
                json={"output_dir": str(root / "output")},
            )
            assert failed.status_code == 500, failed.get_json()
            failed_status = failed.get_json()["recording"]
            assert failed_status["state"] == "ready"
            assert failed_status["staging_dir"] is None
            assert not list((root / "staging").glob("*.recording"))

            brain.start_audio_capture = lambda: None
            retried = client.post(
                "/api/experiment/start",
                json={"output_dir": str(root / "output"), "group_number": 2},
            )
            assert retried.status_code == 201, retried.get_json()
            assert retried.get_json()["recording"]["state"] == "recording"
            brain._experiment_active.clear()
            brain._experiment_ending.clear()
            brain.experiment_recorder.abort_failed_start()
        finally:
            brain.experiment_recorder = original["recorder"]
            brain._microphones = original["microphones"]
            brain._no_mic_mode = original["no_mic"]
            brain.start_audio_capture = original["start_audio"]
            brain.stop_audio_capture = original["stop_audio"]
            brain._experiment_active.clear()
            brain._experiment_ending.clear()
            if original["mqtt"]:
                brain._mqtt_connected.set()
            else:
                brain._mqtt_connected.clear()
            if original["robot"]:
                brain._robot_online.set()
            else:
                brain._robot_online.clear()


def test_flush_discards_only_unmatched_analysis_tail():
    for audio_queue in brain.audio_queues.values():
        while not audio_queue.empty():
            audio_queue.get_nowait()
    brain._brain_flush_request.clear()
    brain._brain_flushed.clear()
    brain.audio_queues["node1"].put(b"\x00\x00" * 16)

    def acknowledge_flush():
        assert brain._brain_flush_request.wait(1)
        brain._brain_flush_request.clear()
        brain._brain_flushed.set()

    worker = threading.Thread(target=acknowledge_flush)
    worker.start()
    try:
        brain.flush_audio_processing(timeout=2)
        assert all(audio_queue.empty() for audio_queue in brain.audio_queues.values())
    finally:
        worker.join(timeout=2)
        brain._brain_flush_request.clear()
        brain._brain_flushed.clear()


def test_overlapping_lifecycle_request_is_rejected():
    client = brain.app.test_client()
    assert brain._experiment_api_lock.acquire(blocking=False)
    try:
        response = client.post("/api/experiment/end")
        assert response.status_code == 409
        assert response.get_json()["status"] == "busy"
    finally:
        brain._experiment_api_lock.release()


if __name__ == "__main__":
    test_http_start_end_export_shutdown_lifecycle()
    test_http_failed_audio_start_is_retryable()
    test_flush_discards_only_unmatched_analysis_tail()
    test_overlapping_lifecycle_request_is_rejected()
    print("Experiment HTTP lifecycle test passed.")
