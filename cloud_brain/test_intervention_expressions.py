# -*- coding: utf-8 -*-
import io
import sys

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding="utf-8",
    errors="replace",
)

from logic.meeting_state import MeetingState


class MockNetwork:
    def __init__(self):
        self.commands = []
        self.expressions = []

    def send_command(self, action, target_node, expression="reminder"):
        command = (action, target_node, expression)
        self.commands.append(command)
        print(f"[MOCK] command={command}")

    def send_expression(self, expression):
        self.expressions.append(expression)
        print(f"[MOCK] expression={expression}")


def test_intervention_expression_escalation():
    config = {
        "logic": {
            "intervention_interval": 120,
            "imbalance_ratio_threshold": 0.5,
        }
    }
    network = MockNetwork()
    state = MeetingState(config, network)

    state.users.update(
        {
            "node1": 100.0,
            "node2": 10.0,
            "node3": 0.0,
            "node4": 15.0,
        }
    )

    state.check_balance()
    assert network.commands[-1] == ("move", "node3", "reminder")
    assert state.intervention_counts["node3"] == 1

    state.check_balance()
    assert network.commands[-1] == ("move", "node3", "curious")
    assert state.intervention_counts["node3"] == 2

    state.check_balance()
    assert network.commands[-1] == ("move", "node3", "curious")
    assert state.intervention_counts["node3"] == 3

    state.users.update(
        {
            "node1": 100.0,
            "node2": 80.0,
            "node3": 80.0,
            "node4": 80.0,
        }
    )
    command_count = len(network.commands)
    state.check_balance()
    assert len(network.commands) == command_count
    assert network.expressions[-1] == "stable"

    print("Intervention expression escalation test passed.")


if __name__ == "__main__":
    test_intervention_expression_escalation()
