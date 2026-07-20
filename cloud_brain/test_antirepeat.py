# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from logic.meeting_state import MeetingState

class MockNetwork:
    def send_command(self, action, target_node):
        print(f"📡 [MOCK NETWORK] 发送指令: action={action}, target_node={target_node}")

def test_anti_repetition():
    print("=== 开始测试防重复干预逻辑 ===")
    config = {
        "logic": {
            "intervention_interval": 120,
            "imbalance_ratio_threshold": 0.5
        }
    }
    network = MockNetwork()
    state = MeetingState(config, network)
    
    # 模拟数据
    # node1: 100.0, node2: 10.0, node3: 0.0, node4: 15.0
    state.users["node1"] = 100.0
    state.users["node2"] = 10.0
    state.users["node3"] = 0.0
    state.users["node4"] = 15.0
    
    print("\n--- 第一轮结算 ---")
    print(f"当前发言时间: {state.users}")
    # 预期干预最沉默的 node3 (0.0s)
    state.check_balance()
    print(f"第一轮后 last_intervention_target: {state.last_intervention_target}")
    assert state.last_intervention_target == "node3", "第一轮应该干预 node3"
    
    print("\n--- 第二轮结算（无时长变化） ---")
    print(f"当前发言时间: {state.users}")
    # 预期最沉默依然是 node3 (0.0s)，但由于上一轮是 node3，应该顺延干预第二沉默的 node2 (10.0s)
    # 校验：平均数 = 125/4 = 31.25. 判定线 = 15.625. node2 (10s) < 15.625, 符合干预条件
    state.check_balance()
    print(f"第二轮后 last_intervention_target: {state.last_intervention_target}")
    assert state.last_intervention_target == "node2", "第二轮应该避开 node3 并干预 node2"

    print("\n--- 第三轮结算（无时长变化） ---")
    print(f"当前发言时间: {state.users}")
    # 预期最沉默是 node3 (0.0s)，上一轮是 node2，所以可以干预 node3
    state.check_balance()
    print(f"第三轮后 last_intervention_target: {state.last_intervention_target}")
    assert state.last_intervention_target == "node3", "第三轮应该避开 node2 并干预 node3"

    print("\n--- 第四轮结算（模拟发言追平） ---")
    # 强行给 node2, node3, node4 增加发言，使大家比较平均
    state.users["node2"] = 80.0
    state.users["node3"] = 80.0
    state.users["node4"] = 80.0
    print(f"当前发言时间: {state.users}")
    # 预期：大家发言比较平均，不触发任何干预，并且 last_intervention_target 保持为 node3 不变
    state.check_balance()
    print(f"第四轮后 last_intervention_target: {state.last_intervention_target}")
    assert state.last_intervention_target == "node3", "第四轮由于分布均衡不应干预，且 target 保持不变"

    print("\n✅ 防重复与发言均等过滤逻辑测试通过！")

if __name__ == "__main__":
    test_anti_repetition()
