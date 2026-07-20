import threading
import time
from collections import deque


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

        self.last_intervention_target = None
        self.recent_intervention_targets = deque(maxlen=1)

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

    def _pick_candidate(self, sorted_nodes):
        """Pick the least-speaking node, or the second least if the last target repeats."""
        last_target = self.recent_intervention_targets[-1] if self.recent_intervention_targets else None
        fallback_node, fallback_time = sorted_nodes[0]

        if fallback_node != last_target:
            return fallback_node, fallback_time, False

        if len(sorted_nodes) > 1:
            node, seconds = sorted_nodes[1]
            return node, seconds, True

        return fallback_node, fallback_time, False

    def check_balance(self):
        """Check whether the speaking distribution is imbalanced."""
        times = list(self.users.values())
        total = sum(times)
        if total <= 5:
            return

        avg_time = total / 4.0
        sorted_nodes = sorted(self.users.items(), key=lambda x: x[1])
        candidate_node, candidate_time, avoided_recent = self._pick_candidate(sorted_nodes)

        if candidate_time < avg_time * self.imbalance_ratio_threshold:
            avoid_msg = ""
            if avoided_recent and self.last_intervention_target:
                avoid_msg = f" (skipped recent target {self.last_intervention_target})"

            print(
                f"[trigger] {candidate_node} is under threshold "
                f"({candidate_time:.1f}s < {avg_time * self.imbalance_ratio_threshold:.1f}s){avoid_msg}"
            )

            self.last_intervention_target = candidate_node
            self.recent_intervention_targets.append(candidate_node)
            self._trigger_intervention(candidate_node)
        else:
            print(
                f"[sched] balanced, no intervention. "
                f"candidate={candidate_node} time={candidate_time:.1f}s "
                f">= {avg_time * self.imbalance_ratio_threshold:.1f}s"
            )

    def _trigger_intervention(self, silent_user):
        """Trigger the intervention action."""
        print(f"[trigger] move to {silent_user}")

        if self.network:
            self.network.send_command(action="move", target_node=silent_user)
