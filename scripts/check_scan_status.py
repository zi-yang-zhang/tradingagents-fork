#!/usr/bin/env python3
"""
扫描状态检测模块 - 避免重复执行扫描

设计原则:
    1. 优先检查今天的日志文件（最可靠，不受 cron 状态刷新延迟影响）
    2. 其次检查 notification_latest.json 的时间戳
    3. 最后检查 cron 输出目录（作为兜底）

用法:
    # 作为模块导入
    from check_scan_status import ScanStatusChecker
    
    checker = ScanStatusChecker()
    status = checker.check()
    if status.scanned_today:
        print("今天已扫描，直接读取结果")
        print(status.latest_result)
    else:
        print("今天未扫描，可以启动新扫描")

    # 命令行使用
    python scripts/check_scan_status.py
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class ScanStatus:
    """扫描状态结果"""
    scanned_today: bool
    log_file: Optional[Path]
    notification_file: Optional[Path]
    latest_result: Optional[str]
    scan_time: Optional[str]
    stocks_found: int
    message: str


class ScanStatusChecker:
    """扫描状态检测器"""

    def __init__(self, logs_dir: str = "/home/ubuntu/tradingagents-fork/logs",
                 results_dir: str = "/home/ubuntu/tradingagents-fork/stock_analysis_result",
                 cron_output_dir: str = "/home/ubuntu/.hermes/cron/output/cbdf4d9ed07e"):
        self.logs_dir = Path(logs_dir)
        self.results_dir = Path(results_dir)
        self.cron_output_dir = Path(cron_output_dir)
        self.today_str = datetime.now().strftime("%Y%m%d")
        self.today_iso = datetime.now().strftime("%Y-%m-%d")

    def _check_today_logs(self) -> tuple[bool, Optional[Path], Optional[str]]:
        """检查今天是否有扫描日志文件"""
        log_pattern = f"cron_scan_{self.today_str}*.log"
        logs = sorted(self.logs_dir.glob(log_pattern), key=lambda p: p.stat().st_mtime, reverse=True)

        if not logs:
            return False, None, None

        latest_log = logs[0]
        stat = latest_log.stat()

        # 检查日志是否有效（非空且包含完成标记）
        if stat.st_size < 100:
            return False, latest_log, "日志文件过小"

        try:
            content = latest_log.read_text(encoding="utf-8", errors="ignore")
            completion_markers = ["扫描完成", "通知摘要", "执行完成", "📤 通知摘要已保存"]
            has_complete = any(marker in content for marker in completion_markers)

            if has_complete:
                scan_time = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S")
                return True, latest_log, scan_time
            else:
                return False, latest_log, "日志未完成"
        except Exception:
            return False, latest_log, "读取日志失败"

    def _check_notification_json(self) -> tuple[bool, Optional[Path], Optional[str], int]:
        """检查 notification_latest.json 是否是今天的"""
        notify_file = self.results_dir / "notification_latest.json"

        if not notify_file.exists():
            return False, None, None, 0

        stat = notify_file.stat()
        file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y%m%d")

        if file_date != self.today_str:
            return False, notify_file, None, 0

        try:
            with open(notify_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            found_count = data.get("found", 0)
            scan_time = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S")
            return True, notify_file, scan_time, found_count
        except (json.JSONDecodeError, Exception):
            return False, notify_file, None, 0

    def _check_cron_output(self) -> tuple[bool, Optional[Path], Optional[str]]:
        """检查 cron 输出目录（兜底方案，可能有延迟）"""
        if not self.cron_output_dir.exists():
            return False, None, None

        md_files = sorted(self.cron_output_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not md_files:
            return False, None, None

        latest = md_files[0]
        stat = latest.stat()
        file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y%m%d")

        if file_date == self.today_str:
            scan_time = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S")
            return True, latest, scan_time

        return False, None, None

    def _get_latest_result(self) -> Optional[str]:
        """获取今天最新的扫描结果摘要"""
        notify_file = self.results_dir / "notification_latest.json"
        if not notify_file.exists():
            return None

        try:
            with open(notify_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("message", "无通知内容")
        except Exception as e:
            return f"读取通知失败: {e}"

    def check(self) -> ScanStatus:
        """
        执行完整的扫描状态检测
        
        优先级:
            1. 今天的日志文件（最可靠）
            2. notification_latest.json
            3. cron 输出目录（兜底）
        """
        # 优先级 1: 检查今天的日志文件
        log_ok, log_file, log_info = self._check_today_logs()
        if log_ok:
            return ScanStatus(
                scanned_today=True,
                log_file=log_file,
                notification_file=self.results_dir / "notification_latest.json",
                latest_result=self._get_latest_result(),
                scan_time=log_info,
                stocks_found=0,  # 从 notification 获取更准确
                message=f"今天已扫描 (日志确认) | 日志: {log_file.name if log_file else 'N/A'}"
            )

        # 优先级 2: 检查 notification_latest.json
        notify_ok, notify_file, notify_time, found_count = self._check_notification_json()
        if notify_ok:
            return ScanStatus(
                scanned_today=True,
                log_file=log_file,
                notification_file=notify_file,
                latest_result=self._get_latest_result(),
                scan_time=notify_time,
                stocks_found=found_count,
                message=f"今天已扫描 (notification 确认) | 发现: {found_count} 只"
            )

        # 优先级 3: 检查 cron 输出（兜底）
        cron_ok, cron_file, cron_time = self._check_cron_output()
        if cron_ok:
            return ScanStatus(
                scanned_today=True,
                log_file=log_file,
                notification_file=notify_file,
                latest_result=self._get_latest_result(),
                scan_time=cron_time,
                stocks_found=0,
                message=f"今天可能已扫描 (cron 输出确认，可能有延迟) | 文件: {cron_file.name if cron_file else 'N/A'}"
            )

        # 未找到今天的扫描记录
        return ScanStatus(
            scanned_today=False,
            log_file=log_file,
            notification_file=notify_file,
            latest_result=None,
            scan_time=None,
            stocks_found=0,
            message="今天尚未执行扫描"
        )


def main():
    """命令行入口"""
    import argparse
    parser = argparse.ArgumentParser(description="扫描状态检测")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    checker = ScanStatusChecker()
    status = checker.check()

    if args.json:
        result = {
            "scanned_today": status.scanned_today,
            "scan_time": status.scan_time,
            "stocks_found": status.stocks_found,
            "log_file": str(status.log_file) if status.log_file else None,
            "notification_file": str(status.notification_file) if status.notification_file else None,
            "message": status.message,
            "latest_result": status.latest_result,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 50)
        print(f"📊 扫描状态检测 | {datetime.now().strftime('%Y-%m-%d')}")
        print("=" * 50)
        print()

        if status.scanned_today:
            print(f"✅ {status.message}")
            if status.scan_time:
                print(f"   扫描时间: {status.scan_time}")
            if status.stocks_found is not None:
                print(f"   发现股票: {status.stocks_found} 只")
            print()
            if status.latest_result:
                print("📋 最新扫描结果:")
                print("-" * 50)
                print(status.latest_result)
        else:
            print(f"❌ {status.message}")
            print()
            print("💡 建议操作:")
            print("   1. 等待 cron 定时任务自动执行")
            print("   2. 或手动执行: bash ~/TradingAgents/scripts/cron_daily_scan.sh")

    # 返回退出码: 0=已扫描, 1=未扫描
    return 0 if status.scanned_today else 1


if __name__ == "__main__":
    exit(main())
