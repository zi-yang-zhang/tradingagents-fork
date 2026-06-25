#!/usr/bin/env python3
"""A-股每日定时市场扫描 (完整版)

调用 ashare-analyzer 中的 scan_ashare.py：
    Layer 1: AkShare 全市场筛选（约 30 秒）
    Layer 2: Top N 逐一浅度 LLM 分析（每只约 2-3 分钟）

功能:
    1. 检查今日是否为交易日（非交易日自动跳过）
    2. 运行 scan_ashare.py 完整流程
    3. 输出汇总报告路径

用法:
    cd ~/TradingAgents && source .venv/bin/activate
    python scripts/cron_market_scan.py
"""

import subprocess
import sys
from datetime import datetime

sys.path.insert(0, "/home/ubuntu/tradingagents-fork")


def is_trading_day() -> bool:
    """检查今天是否为 A 股交易日（周末 + 实时数据验证）."""
    try:
        import akshare as ak
    except ImportError:
        print("❌ akshare 未安装")
        return False

    today = datetime.now()

    # 周末直接排除
    if today.weekday() >= 5:
        return False

    # 尝试获取实时行情验证是否开市
    try:
        df = ak.stock_zh_a_spot_em()
        return df is not None and len(df) > 100
    except Exception as e:
        print(f"⚠️ 交易日检查接口异常: {e}")
        # 保守处理：假设是交易日（cron 已排除周末）
        return True


def main():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d %H:%M")

    # 1. 交易日检查
    if not is_trading_day():
        print(f"📅 {today_str} | 今日非交易日，跳过市场扫描")
        return

    print(f"📊 {today_str} | A股市场机会扫描 (完整版)")
    print("━" * 80)
    print("使用脚本: scan_ashare.py （ashare-analyzer skill）")
    print("流程: Layer 1 快速筛选 → Layer 2 浅度 LLM 分析")
    print("预计耗时: 10-15 分钟")
    print("━" * 80)

    # 2. 运行 scan_ashare.py
    try:
        result = subprocess.run(
            ["python", "scan_ashare.py"],
            cwd="/home/ubuntu/tradingagents-fork",
            capture_output=True,
            text=True,
            timeout=1200,  # 20 分钟超时
        )

        # 输出标准输出
        if result.stdout:
            print(result.stdout)

        # 输出错误输出（如果有）
        if result.stderr:
            print("\n⚠️ STDERR:")
            print(result.stderr)

        # 返回码非零则警告
        if result.returncode != 0:
            print(f"\n❌ 扫描脚本退出码: {result.returncode}")

    except subprocess.TimeoutExpired:
        print("\n❌ 扫描超时（超过 20 分钟），可能是 LLM API 响应慢或市场数据拉取异常")
    except Exception as e:
        print(f"\n❌ 扫描执行失败: {e}")


if __name__ == "__main__":
    main()
