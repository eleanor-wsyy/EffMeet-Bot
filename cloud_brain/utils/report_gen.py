import datetime
import os

import pandas as pd


class ReportGenerator:
    def __init__(self, meeting_state):
        self.meeting_state = meeting_state
        self.report_dir = "data/logs"
        os.makedirs(self.report_dir, exist_ok=True)

    def generate_excel_report(self):
        print("\n[system] Generating meeting report...")

        users_data = self.meeting_state.users
        total_time = sum(users_data.values())

        if total_time == 0:
            print("[warning] Total speaking time is 0; skipping report generation.")
            return

        records = []
        for user_id, duration in users_data.items():
            percentage = (duration / total_time) * 100
            records.append(
                {
                    "Participant ID": user_id,
                    "Speaking Time (s)": round(duration, 1),
                    "Share (%)": round(percentage, 1),
                }
            )

        df = pd.DataFrame(records)
        most_active = df.loc[df["Speaking Time (s)"].idxmax()]["Participant ID"]
        most_silent = df.loc[df["Speaking Time (s)"].idxmin()]["Participant ID"]

        print(f"[summary] Most active: {most_active}; quietest: {most_silent}.")

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(self.report_dir, f"EffMeet_Report_{timestamp}.xlsx")

        df.to_excel(file_path, index=False, sheet_name="Meeting Stats")
        print(f"[success] Report saved to: {file_path}\n")
