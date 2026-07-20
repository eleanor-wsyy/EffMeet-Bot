import io
import sys
import time
import traceback
from pathlib import Path

import yaml

from logic.meeting_state import MeetingState
from network.mqtt_manager import MQTTManager
from utils.audio_buffer import AudioStreamManager
from utils.report_gen import ReportGenerator


if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )


BASE_DIR = Path(__file__).resolve().parent


def load_config(config_path=BASE_DIR / "config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    print("=== EffMeet-Bot cloud brain starting ===")

    config = load_config()
    network = MQTTManager(config, None)
    meeting_state = MeetingState(config, network)
    audio_stream = AudioStreamManager(meeting_state=meeting_state)
    network.audio_stream = audio_stream

    network.start()

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[system] Shutdown signal received.")

        try:
            report_gen = ReportGenerator(meeting_state)
            report_gen.generate_excel_report()
        except Exception as e:
            print(f"[warning] Failed to generate report: {e}")

        print("[system] Stopping network service.")
        network.client.loop_stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\nPress Enter to exit...")
