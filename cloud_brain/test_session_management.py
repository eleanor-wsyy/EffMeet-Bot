# -*- coding: utf-8 -*-
import json
import tempfile
from pathlib import Path

import main_brain as brain


def test_archive_and_reset():
    with tempfile.TemporaryDirectory() as temp_dir:
        original_archive_dir = brain.SESSION_ARCHIVE_DIR
        brain.SESSION_ARCHIVE_DIR = Path(temp_dir)
        try:
            first = brain.reset_runtime_state(archive_current=False)
            first_session_id = first["session_id"]

            with brain._cycle_lock:
                with brain.state_lock:
                    brain.speaking_times.update(
                        {"node1": 12.5, "node2": 4.0, "node3": 0.5, "node4": 8.0}
                    )
                    brain.meeting_records.append(
                        {"node": "node1", "time": "10:00:00", "text": "测试"}
                    )
                    brain.speaking_events.append(
                        {"time": "10:00:01", "node": "node1", "add_seconds": 0.5}
                    )
                    brain._intervention_counts["node3"] = 2

            client = brain.app.test_client()
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert "归档并开始下一组" in dashboard.get_data(as_text=True)

            response = client.post("/api/session/reset")
            assert response.status_code == 200, response.get_json()
            payload = response.get_json()
            assert payload["status"] == "success"
            assert payload["session_id"] != first_session_id

            archive_path = Path(payload["archive_path"])
            assert archive_path.exists()
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            assert archive["session_id"] == first_session_id
            assert archive["speaking_times"]["node1"] == 12.5
            assert archive["intervention_counts"]["node3"] == 2
            assert archive["meeting_records"][0]["text"] == "测试"

            status = client.get("/api/get_meeting_data").get_json()
            assert status["total_speaking_time"] == 0
            assert all(value == 0 for value in status["intervention_counts"].values())
            assert status["session_id"] == payload["session_id"]
        finally:
            brain.SESSION_ARCHIVE_DIR = original_archive_dir


def test_busy_robot_blocks_reset():
    brain.reset_runtime_state(archive_current=False)
    with brain._cycle_lock:
        brain._robot_busy = True
        brain._robot_task_started_at = 1.0

    try:
        response = brain.app.test_client().post("/api/session/reset")
        assert response.status_code == 409
        assert response.get_json()["status"] == "busy"
    finally:
        with brain._cycle_lock:
            brain._robot_busy = False
            brain._robot_task_started_at = 0.0


def test_stale_done_does_not_complete_current_task():
    class MockClient:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload):
            self.published.append((topic, payload))

    client = MockClient()
    brain.reset_runtime_state(archive_current=False)
    with brain._cycle_lock:
        brain._robot_busy = True
        brain._cycle_index = 0
        brain._robot_task_started_at = 1.0
        brain._active_interventions[:] = [(3, "reminder")]

    brain._on_robot_done(client, "done|dir=1|target=2|expression=reminder")
    with brain._cycle_lock:
        assert brain._robot_busy is True

    brain._on_robot_done(client, "done|dir=1|target=3|expression=reminder")
    with brain._cycle_lock:
        assert brain._robot_busy is False
        assert not brain._active_interventions
    assert (brain.MQTT_TOPIC_CYCLE_DONE, "cycle_done") in client.published


if __name__ == "__main__":
    test_archive_and_reset()
    test_busy_robot_blocks_reset()
    test_stale_done_does_not_complete_current_task()
    print("Session archive/reset tests passed.")
