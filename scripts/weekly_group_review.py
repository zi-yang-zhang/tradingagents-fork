#!/usr/bin/env python3
"""周五收市后分组回顾分析

流程:
    1. 读取本地"AI智选"分组的所有股票
    2. 对每只逐一浅度分析
    3. 提取评级: SELL/UNDERWEIGHT → 移除
    4. BUY/OVERWEIGHT/HOLD → 保留
    5. 生成回顾报告

用法:
    cd ~/TradingAgents && source .venv/bin/activate
    python scripts/weekly_group_review.py [--group AI智选] [--dry-run]
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/ubuntu/mx-zixuan")
sys.path.insert(0, "/home/ubuntu/tradingagents-fork")

from stock_group_manager import (
    _load_groups, _save_groups, _mx_remove,
)

# ── 配置 ──────────────────────────────────────────
TARGET_GROUP = "AI智选"
REMOVE_RATINGS = {"SELL", "UNDERWEIGHT"}  # 应移除的评级
KEEP_RATINGS = {"BUY", "OVERWEIGHT", "HOLD", "UNKNOWN"}  # 保留的评级
RESULTS_DIR = Path("/home/ubuntu/tradingagents-fork/stock_analysis_result")


def _extract_rating(decision_text: str) -> str:
    """从 Portfolio Manager 决策文本中提取评级."""
    if not decision_text:
        return "UNKNOWN"
    text = decision_text.upper()
    for keyword in ["SELL", "UNDERWEIGHT", "HOLD", "OVERWEIGHT", "BUY"]:
        if keyword in text:
            return keyword
    return "UNKNOWN"


def _analyze_stock(ticker: str) -> Dict[str, Any]:
    """
    对单只股票进行分析.
    返回: {"ticker", "rating", "decision", "report_file", "success"}
    """
    from analyze_ashare import _run_analysis

    print(f"\n   🔍 分析 {ticker}...")
    try:
        _run_analysis(
            user_input=ticker,
            mode="medium",
            analysts="all",
        )

        # 读取最新报告
        report_files = sorted(
            RESULTS_DIR.glob(f"{ticker}_*_report.json"),
            reverse=True,
        )
        if not report_files:
            print("      ⚠️ 未生成报告")
            return {
                "ticker": ticker,
                "rating": "UNKNOWN",
                "decision": "未生成报告",
                "report_file": None,
                "success": False,
            }

        with open(report_files[0], "r", encoding="utf-8") as f:
            report = json.load(f)

        decision = report.get("final_trade_decision", "")
        rating = _extract_rating(decision)

        print(f"      评级: {rating}")
        print(f"      决策: {decision[:80]}...")

        return {
            "ticker": ticker,
            "rating": rating,
            "decision": decision[:200],
            "report_file": str(report_files[0]),
            "success": True,
        }

    except Exception as e:
        print(f"      ❌ 分析失败: {e}")
        return {
            "ticker": ticker,
            "rating": "ERROR",
            "decision": f"分析失败: {e}",
            "report_file": None,
            "success": False,
        }


def _execute_removals(group_name: str, to_remove: List[Dict[str, Any]], dry_run: bool) -> Dict[str, List]:
    """执行移除操作."""
    data = _load_groups()
    if group_name not in data["groups"]:
        return {"removed": [], "failed": []}

    existing = set(data["groups"][group_name].get("stocks", []))
    results = {"removed": [], "failed": []}

    for item in to_remove:
        ticker = item["ticker"]
        reason = item["reason"]

        if ticker not in existing:
            print(f"      ⏭️  {ticker} 不在本地分组中")
            continue

        if dry_run:
            print(f"      🧪 [试运行] 将移除 {ticker} (原因: {reason})")
            results["removed"].append(ticker)
            continue

        print(f"      📤 从云端移除 {ticker}...")
        if _mx_remove(ticker, group_name):
            existing.discard(ticker)
            results["removed"].append(ticker)
            print(f"      ✅ {ticker} 移除成功")
        else:
            results["failed"].append(ticker)
            print(f"      ❌ {ticker} 云端移除失败")

    if not dry_run:
        data["groups"][group_name]["stocks"] = sorted(list(existing))
        _save_groups(data)

    return results


def _generate_review_report(
    group_name: str,
    all_results: List[Dict[str, Any]],
    removal_results: Dict[str, List],
    dry_run: bool,
    elapsed: float,
) -> str:
    """生成回顾报告."""
    lines = []
    lines.append("\n" + "=" * 70)
    lines.append(" 📊 周五分组回顾分析报告")
    lines.append("=" * 70)
    lines.append(f" 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f" 耗时: {elapsed / 60:.1f} 分钟")
    lines.append(f" 分组: {group_name}")
    lines.append(f" 模式: {'试运行' if dry_run else '真实操作'}")
    lines.append("")

    # 分类统计
    keep_list = []
    remove_list = []
    error_list = []
    for r in all_results:
        if r["rating"] in REMOVE_RATINGS:
            remove_list.append(r)
        elif r["rating"] == "ERROR":
            error_list.append(r)
        else:
            keep_list.append(r)

    lines.append(f"📊 分组内共 {len(all_results)} 只股票分析结果:")
    lines.append(f"   ✅ 建议保留: {len(keep_list)} 只")
    lines.append(f"   ❌ 建议移除: {len(remove_list)} 只")
    lines.append(f"   ⚠️ 分析异常: {len(error_list)} 只")
    lines.append("")

    # 保留的
    if keep_list:
        lines.append(f"🐝 保留的股票 ({len(keep_list)} 只):")
        for r in keep_list:
            lines.append(f"   • {r['ticker']} → 评级: {r['rating']}")
        lines.append("")

    # 移除的
    if remove_list:
        lines.append(f"🐜 建议移除的股票 ({len(remove_list)} 只):")
        for r in remove_list:
            lines.append(f"   • {r['ticker']} → 评级: {r['rating']}")
            lines.append(f"      决策: {r['decision'][:60]}...")
        lines.append("")

    # 执行结果
    removed = removal_results.get("removed", [])
    failed = removal_results.get("failed", [])
    lines.append("📂 移除执行结果:")
    lines.append(f"   ✅ 成功移除: {len(removed)} 只")
    for t in removed:
        lines.append(f"      - {t}")
    if failed:
        lines.append(f"   ❌ 移除失败: {len(failed)} 只")
        for t in failed:
            lines.append(f"      ✗ {t}")
    lines.append("")

    # 当前分组状态
    data = _load_groups()
    group = data.get("groups", {}).get(group_name, {})
    all_stocks = group.get("stocks", [])
    lines.append(f"📁 当前 '{group_name}' 分组共 {len(all_stocks)} 只股票:")
    for i, t in enumerate(all_stocks, 1):
        lines.append(f"   {i}. {t}")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="周五分组回顾分析")
    parser.add_argument("--group", default=TARGET_GROUP, help="目标分组名称")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际移除")
    parser.add_argument("--max", type=int, default=0, help="最多分析N只（0=全部）")
    args = parser.parse_args()

    group_name = args.group
    start_time = time.time()

    print("\n" + "🚀" * 35)
    print("  周五收市后分组回顾分析")
    print("  " + "=" * 66)

    # Step 1: 读取分组
    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"\n❌ 分组 '{group_name}' 不存在")
        return

    stocks = data["groups"][group_name].get("stocks", [])
    if not stocks:
        print(f"\n📭 分组 '{group_name}' 为空")
        return

    print(f"\n📁 分组 '{group_name}' 共 {len(stocks)} 只股票")
    if args.max > 0:
        stocks = stocks[:args.max]
        print(f"   本次分析前 {args.max} 只")

    # Step 2: 逐一分析
    print("\n🔍 开始逐一分析 (每只约2-3分钟)...")
    all_results = []
    for i, ticker in enumerate(stocks, 1):
        print(f"\n[{i}/{len(stocks)}] 分析 {ticker}")
        result = _analyze_stock(ticker)
        all_results.append(result)

    # Step 3: 决定移除列表
    to_remove = [
        {"ticker": r["ticker"], "reason": f"评级 {r['rating']}: {r['decision'][:60]}"}
        for r in all_results
        if r["rating"] in REMOVE_RATINGS
    ]

    print(f"\n📂 发现 {len(to_remove)} 只应移除的股票")
    for item in to_remove:
        print(f"   ❌ {item['ticker']} → {item['reason']}")

    # Step 4: 执行移除
    removal_results = _execute_removals(group_name, to_remove, args.dry_run)

    # Step 5: 生成报告
    elapsed = time.time() - start_time
    report = _generate_review_report(
        group_name, all_results, removal_results, args.dry_run, elapsed,
    )
    print(report)

    # 保存报告
    report_path = (
        RESULTS_DIR
        / f"weekly_review_{group_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    report_path.write_text(report, encoding="utf-8")
    print(f"\n📄 报告已保存: {report_path}")

    # 保存通知摘要
    _save_notification(group_name, all_results, removal_results, dry_run)

    # Step 6: AceTrading 持仓回顾调仓（收盘后只生成 pending，不实际执行）
    print("\n" + "=" * 70)
    print(" 🔗 同步到 AceTrading 模拟组合...")
    print("   模式: 收盘后分析 | 指令将在下一个交易日执行")
    print("=" * 70)
    _run_acetrading_review(all_results, dry_run)


def _run_acetrading_review(all_results: List[Dict[str, Any]], dry_run: bool):
    """调用 AceTrading 执行模拟调仓（收盘后只生成 pending，不实际执行）"""
    review_script = Path.home() / "AceTrading/scripts/review_adapter.py"
    if not review_script.exists():
        print(f"   ⚠️ review_adapter.py 不存在，跳过: {review_script}")
        return

    import subprocess
    cmd = [
        sys.executable,
        str(review_script),
        "--pending-only",
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(Path.home() / "AceTrading"),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": str(Path.home() / "AceTrading")},
        )
        if result.returncode == 0:
            print("   ✅ AceTrading 待执行指令已生成")
            print(result.stdout[-800:] if len(result.stdout) > 800 else result.stdout)
        else:
            print(f"   ⚠️ AceTrading 返回非零: {result.returncode}")
            print(result.stderr[:400])
    except Exception as e:
        print(f"   ❌ AceTrading 调仓失败: {e}")


def _save_notification(
    group_name: str,
    all_results: List[Dict[str, Any]],
    removal_results: Dict[str, List],
    dry_run: bool,
):
    """保存精简通知摘要为 JSON，供 cron 发送 QQ 消息用."""
    keep_list = [r for r in all_results if r["rating"] not in REMOVE_RATINGS and r["rating"] != "ERROR"]
    remove_list = [r for r in all_results if r["rating"] in REMOVE_RATINGS]
    error_list = [r for r in all_results if r["rating"] == "ERROR"]
    removed = removal_results.get("removed", [])
    failed = removal_results.get("failed", [])

    lines = []
    lines.append(f"📊 周五分组回顾 | {datetime.now().strftime('%m-%d %H:%M')}")
    lines.append(f"📁 分组: {group_name}")
    if dry_run:
        lines.append("🧪 模式: 试运行")
    lines.append("")
    lines.append(f"📊 共分析 {len(all_results)} 只:")
    lines.append(f"  ✅ 保留 {len(keep_list)} 只  |  ❌ 移除 {len(remove_list)} 只  |  ⚠️ 异常 {len(error_list)} 只")
    lines.append("")

    if remove_list:
        lines.append("🐜 建议移除:")
        for r in remove_list:
            lines.append(f"  ✗ {r['ticker']} → {r['rating']}")
        lines.append("")

    if removed:
        lines.append(f"📂 已执行移除: {len(removed)} 只")
        for t in removed:
            lines.append(f"  - {t}")
    if failed:
        lines.append(f"❌ 移除失败: {len(failed)} 只")
        for t in failed:
            lines.append(f"  ✗ {t}")

    # 当前分组总数
    data = _load_groups()
    group = data.get("groups", {}).get(group_name, {})
    total = len(group.get("stocks", []))
    lines.append("")
    lines.append(f"📁 当前'{group_name}'共 {total} 只")

    summary_text = "\n".join(lines)

    notification = {
        "type": "weekly_review",
        "timestamp": datetime.now().isoformat(),
        "group": group_name,
        "total": len(all_results),
        "keep": len(keep_list),
        "remove": len(remove_list),
        "error": len(error_list),
        "removed_count": len(removed),
        "failed_count": len(failed),
        "total_in_group": total,
        "dry_run": dry_run,
        "removed_tickers": removed,
        "message": summary_text,
    }

    notify_path = RESULTS_DIR / "notification_latest.json"
    notify_path.write_text(json.dumps(notification, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📤 通知摘要已保存: {notify_path}")


if __name__ == "__main__":
    main()
