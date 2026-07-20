import threading
import time


class MeetingState:
    def __init__(self, config, network):
        self.config = config
        self.network = network

        self.users = {
            "node1": 0.0,
            "node2": 0.0,
            "node3": 0.0,
            "node4": 0.0,
        }

        logic_config = config.get("logic", {}) if config else {}
        self.intervention_interval = logic_config.get("intervention_interval", 120)
        self.imbalance_ratio_threshold = logic_config.get("imbalance_ratio_threshold", 0.5)

        self.intervention_counts = {node: 0 for node in self.users}

        threading.Thread(target=self._periodic_check_loop, daemon=True).start()

    def add_speech_time(self, device_id, duration):
        """Record speaking duration."""
        if device_id in self.users:
            self.users[device_id] += duration
            print(f"[stats] {device_id} total speaking: {self.users[device_id]:.1f}s")

    def _periodic_check_loop(self):
        """Periodic balance check loop."""
        while True:
            time.sleep(self.intervention_interval)
            self.check_balance()

    def check_balance(self):
        """Check whether the speaking distribution is imbalanced."""
        times = list(self.users.values())
        total = sum(times)
        if total <= 5:
            if self.network:
                self.network.send_expression("stable")
            return

        avg_time = total / 4.0
        sorted_nodes = sorted(self.users.items(), key=lambda x: x[1])
        candidate_node, candidate_time = sorted_nodes[0]

        if candidate_time < avg_time * self.imbalance_ratio_threshold:
            self.intervention_counts[candidate_node] += 1
            intervention_count = self.intervention_counts[candidate_node]
            expression = "reminder" if intervention_count == 1 else "curious"

            print(
                f"[trigger] {candidate_node} is under threshold "
                f"({candidate_time:.1f}s < {avg_time * self.imbalance_ratio_threshold:.1f}s); "
                f"intervention={intervention_count} expression={expression}"
            )
            self._trigger_intervention(candidate_node, expression)
        else:
            print(
                f"[sched] balanced, no intervention. "
                f"candidate={candidate_node} time={candidate_time:.1f}s "
                f">= {avg_time * self.imbalance_ratio_threshold:.1f}s"
            )
            if self.network:
                self.network.send_expression("stable")

    def _trigger_intervention(self, silent_user, expression):
        """Trigger the intervention action."""
        print(f"[trigger] move to {silent_user} with {expression} expression")

        if self.network:
            self.network.send_command(
                action="move",
                target_node=silent_user,
                expression=expression,
            )
