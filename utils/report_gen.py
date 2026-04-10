import pandas as pd
import datetime
import os

class ReportGenerator:
    def __init__(self, meeting_state):
        self.meeting_state = meeting_state
        self.report_dir = "data/logs"
        
        # 确保日志文件夹存在
        os.makedirs(self.report_dir, exist_ok=True)

    def generate_excel_report(self):
        """生成并导出会议数据分析 Excel"""
        print("\n[系统] 正在生成会议数据洞察报告...")
        
        # 1. 获取核心数据
        users_data = self.meeting_state.users
        total_time = sum(users_data.values())
        
        # 2. 如果根本没人说话，就不生成了
        if total_time == 0:
            print("[警告] 会议总发言时长为 0，跳过报告生成。")
            return
            
        # 3. 数据重组与基础分析
        records = []
        for user_id, duration in users_data.items():
            # 计算每个人发言占总时长的百分比
            percentage = (duration / total_time) * 100
            records.append({
                "参会者 ID": user_id,
                "发言总时长 (秒)": round(duration, 1),
                "发言占比 (%)": round(percentage, 1)
            })
            
        # 4. 转换为 Pandas DataFrame，方便操作
        df = pd.DataFrame(records)
        
        # 5. 添加一些高阶的统计结论
        most_active = df.loc[df['发言总时长 (秒)'].idxmax()]['参会者 ID']
        most_silent = df.loc[df['发言总时长 (秒)'].idxmin()]['参会者 ID']
        
        print(f"📊 会议小结：最活跃的是 {most_active}，最沉默的是 {most_silent}。")
        
        # 6. 生成带时间戳的文件名
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(self.report_dir, f"EffMeet_Report_{timestamp}.xlsx")
        
        # 7. 导出为 Excel 文件
        df.to_excel(file_path, index=False, sheet_name="会议发言统计")
        print(f"✅ [成功] 报告已保存至: {file_path}\n")