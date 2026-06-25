#!/usr/bin/env python3
"""每日市场扫描 + 自动添加好股到AI智选分组

流程:
    1. 运行 scan_ashare.py 完整扫描+浅度分析
    2. 从结果中提取 BUY / OVERWEIGHT 评级的股票
    3. 自动添加到本地"AI智选"分组 + 同步到东方财富云端
    4. 生成操作报告

用法:
    cd ~/TradingAgents && source .venv/bin/activate
    python scripts/daily_scan_and_add.py [--group AI智选]
"""

import json
import subprocess
import sys
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/ubuntu/mx-zixuan")
sys.path.insert(0, "/home/ubuntu/tradingagents-fork")

from stock_group_manager import _load_groups, _save_groups, _normalize_ticker, _mx_add


# ── 配置 ──────────────────────────────────────────
TARGET_GROUP = "AI智选"
MIN_RATING = {"BUY", "OVERWEIGHT"}  # 只添加这两个评级的股票
RESULTS_DIR = Path("/home/ubuntu/tradingagents-fork/stock_analysis_result")
SCAN_SCRIPT = Path("/home/ubuntu/tradingagents-fork/scan_ashare.py")


def _extract_ticker_from_report(report_path: Path) -> str:
    """从报告文件名提取 ticker (e.g. 601138.SS_2026-05-15_medium_report.json)"""
    stem = report_path.stem
    parts = stem.split("_")
    if parts:
        return parts[0]
    return ""


def _run_scan(strategy: str = "balanced", top_n: int = 10) -> List[Dict[str, Any]]:
    """运行 scan_ashare.py 并返回分析结果列表."""
    print("\n" + "=" * 70)
    print(f"📊 阶段 1/3: 运行市场扫描 (档位: {strategy})")
    print("=" * 70)

    start = time.time()
    # 确保 MX 优先，避免回退到 westock-data (npx 在 cron 环境可能缺失/慢)
    env = os.environ.copy()
    env["STOCK_DATA_HUB_MX_FIRST"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, str(SCAN_SCRIPT), "--strategy", strategy, "--top-n", str(top_n)],
            cwd="/home/ubuntu/tradingagents-fork",
            capture_output=True,
            text=True,
            timeout=1800,  # 30分钟
            env=env,
        )
        print(result.stdout)
        if result.stderr:
            print("\n⚠️ STDERR:")
            print(result.stderr)
        if result.returncode != 0:
            print(f"\n❌ scan_ashare.py 退出码: {result.returncode}")
            return []
    except subprocess.TimeoutExpired:
        print("\n❌ 扫描超时（超过30分钟）")
        return []
    except Exception as e:
        print(f"\n❌ 扫描执行失败: {e}")
        return []

    # 从最新汇总报告中提取结果
    summary_files = sorted(RESULTS_DIR.glob("scan_summary_*.txt"), reverse=True)
    if not summary_files:
        print("\n⚠️ 未找到汇总报告，尝试从JSON报告反推...")
        return _parse_from_json_reports()

    # 从最新汇总报告中提取结果
    scan_results = _parse_summary_text(summary_files[0])
    # JSON fallback: 如果文本解析为空，但 JSON 中有 BUY/OVERWEIGHT，使用 JSON
    if not scan_results:
        print("\n⚠️  文本解析未找到 BUY/OVERWEIGHT，尝试 JSON fallback...")
        scan_results = _parse_from_json_candidates()
        if scan_results:
            print(f"   ✅ JSON fallback 找到 {len(scan_results)} 只 BUY/OVERWEIGHT")
    return scan_results


def _load_scan_candidates() -> Dict[str, Dict[str, Any]]:
    """加载最新的候选股票JSON，返回 code -> info 映射."""
    latest_json = RESULTS_DIR / "scan_candidates_latest.json"
    if not latest_json.exists():
        return {}
    try:
        with open(latest_json, "r", encoding="utf-8") as f:
            candidates = json.load(f)
        return {c["code"]: c for c in candidates if c.get("code")}
    except Exception as e:
        print(f"   ⚠️ 读取候选JSON失败: {e}")
        return {}

def _parse_summary_text(summary_path: Path) -> List[Dict[str, Any]]:
    """从汇总报告文本中提取BUY/OVERWEIGHT的股票，并整合推荐理由."""
    lines = summary_path.read_text(encoding="utf-8").splitlines()
    good_stocks = []
    candidates_map = _load_scan_candidates()

    current = {}
    in_decision_block = False
    decision_lines = []
    
    for line in lines:
        line_stripped = line.strip()
        # 匹配: "1. 工业富联 (601138)" 或 "### 5. 云铝股份 (000807)"
        cleaned = line_stripped.lstrip("#").strip()
        if cleaned and cleaned[0].isdigit() and "." in cleaned[:5]:
            # 处理上一个股票（如果有）
            if current and decision_lines:
                # 从决策文本中提取真实评级（覆盖汇总行可能错误的评级）
                real_rating = _extract_rating_from_text("\n".join(decision_lines))
                if real_rating != "UNKNOWN":
                    current["rating"] = real_rating
                
                rating = current.get("rating", "")
                if rating in MIN_RATING:
                    _append_good_stock(current, good_stocks, candidates_map)
            
            parts = cleaned.split()
            if len(parts) >= 3 and "(" in cleaned and ")" in cleaned:
                # 提取名称和代码
                code_part = cleaned.split("(")[-1].split(")")[0]
                name_part = cleaned.split(".")[-1].split("(")[0].strip()
                current = {"name": name_part, "code": code_part}
                in_decision_block = False
                decision_lines = []
        # 匹配评级行（汇总行，可能不准确）
        # 支持两种格式: "→ 评级: OVERWEIGHT" 或 "| 评级: **OVERWEIGHT**"
        elif any(k in line_stripped for k in ("→ 评级:", "| 评级:")) and current:
            rating = ""
            for sep in ("→ 评级:", "| 评级:"):
                if sep in line_stripped:
                    rating = line_stripped.split(sep)[-1].strip()
                    break
            # 去除 markdown 粗体标记 **BUY** → BUY
            rating = rating.replace("**", "").strip()
            # 只保留评级关键词（去除后面的 Action/其他内容）
            for r in ["SELL", "UNDERWEIGHT", "HOLD", "OVERWEIGHT", "BUY"]:
                if r in rating.upper():
                    current["rating"] = r
                    break
            in_decision_block = False
            decision_lines = []
        # 匹配决策行，开始收集决策文本
        # 支持 "→ 决策:" 或 "**多空摘要**" 格式
        # 注意: "**核心依据**" 不作为触发器，它是决策块的内容，会在 in_decision_block 中被收集
        elif any(k in line_stripped for k in ("→ 决策:", "**多空摘要**")) and current and not in_decision_block:
            if "→ 决策:" in line_stripped:
                decision_text = line_stripped.split("→ 决策:")[-1].strip()
            else:
                decision_text = line_stripped
            decision_lines = [decision_text]
            in_decision_block = True
        elif in_decision_block and line_stripped and not line_stripped.startswith("=") and not (line_stripped and line_stripped[0].isdigit() and "." in line_stripped[:5]):
            # 继续收集决策文本块，直到遇到下一个股票或分隔线
            decision_lines.append(line_stripped)
        elif in_decision_block and (line_stripped.startswith("=") or (line_stripped and line_stripped[0].isdigit() and "." in line_stripped[:5])):
            in_decision_block = False
    
    # 处理最后一个股票
    if current and decision_lines:
        real_rating = _extract_rating_from_text("\n".join(decision_lines))
        if real_rating != "UNKNOWN":
            current["rating"] = real_rating
        
        rating = current.get("rating", "")
        if rating in MIN_RATING:
            _append_good_stock(current, good_stocks, candidates_map)

    return good_stocks


def _extract_rating_from_text(text: str) -> str:
    """从决策文本中提取真实评级，优先匹配更具体的评级关键词."""
    if not text:
        return "UNKNOWN"
    text_upper = text.upper()
    # 按优先级顺序检查（更具体的先匹配）
    for keyword in ["SELL", "UNDERWEIGHT", "HOLD", "OVERWEIGHT", "BUY"]:
        if keyword in text_upper:
            return keyword
    return "UNKNOWN"


def _append_good_stock(current: Dict, good_stocks: List, candidates_map: Dict):
    """将符合条件的股票加入 good_stocks 列表，并整合推荐理由."""
    code = current.get("code", "")
    cand = candidates_map.get(code, {})
    
    # 优先使用浅度分析结论
    analysis_reason = cand.get("analysis_reason", "")
    if analysis_reason:
        current["reason"] = analysis_reason
    else:
        # fallback 到评分维度
        score_details = cand.get("score_details", [])
        if score_details:
            current["reason"] = "；".join(score_details)
        else:
            # 备用：用基础数据构建简单推荐理由
            parts = []
            chg = cand.get("change_pct")
            if chg is not None:
                parts.append(f"涨跌幅{chg:+.2f}%")
            inf = cand.get("main_inflow_million")
            if inf is not None:
                parts.append(f"主力流入{inf:+.0f}万")
            rsi = cand.get("rsi")
            if rsi is not None:
                parts.append(f"RSI{rsi:.1f}")
            score = cand.get("score")
            if score is not None:
                parts.append(f"综合分{score:.1f}")
            current["reason"] = "；".join(parts) if parts else "筛选模型推荐"
    
    good_stocks.append(current.copy())


def _parse_from_json_reports() -> List[Dict[str, Any]]:
    """当没有汇总报告时，直接从最新JSON报告提取."""
    good_stocks = []
    json_files = sorted(RESULTS_DIR.glob("*_report.json"), reverse=True)

    # 只取今天的报告
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_files = [f for f in json_files if today_str in f.name]

    for report_path in today_files:
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            decision = report.get("final_trade_decision", "")
            ticker = report.get("ticker", "")
            if not ticker:
                ticker = _extract_ticker_from_report(report_path)

            rating = _extract_rating(decision)
            if rating in MIN_RATING:
                good_stocks.append({
                    "name": report.get("company_name", ticker),
                    "code": ticker.split(".")[0],
                    "rating": rating,
                })
        except Exception as e:
            print(f"   ⚠️  解析报告失败 {report_path.name}: {e}")

    return good_stocks


def _parse_from_json_candidates() -> List[Dict[str, Any]]:
    """从 scan_candidates_latest.json 中提取 BUY/OVERWEIGHT 的股票.

    作为文本解析失败时的 JSON fallback，直接读取候选JSON中的
    analysis_reason 字段提取评级，避免文本格式变化导致漏加。
    """
    candidates_map = _load_scan_candidates()
    if not candidates_map:
        return []

    good_stocks = []
    for code, cand in candidates_map.items():
        analysis_reason = cand.get("analysis_reason", "")
        if not analysis_reason:
            continue
        rating = _extract_rating(analysis_reason)
        if rating in MIN_RATING:
            good_stocks.append({
                "name": cand.get("name", code),
                "code": code,
                "rating": rating,
            })
    return good_stocks


def _extract_rating(decision_text: str) -> str:
    """从决策文本中提取评级."""
    if not decision_text:
        return "UNKNOWN"
    text = decision_text.upper()
    for keyword in ["SELL", "UNDERWEIGHT", "HOLD", "OVERWEIGHT", "BUY"]:
        if keyword in text:
            return keyword
    return "UNKNOWN"


def _add_to_group(stocks: List[Dict[str, Any]], group_name: str, skip_cloud_sync: bool = True) -> Dict[str, Any]:
    """将股票添加到指定分组（本地更新 + 记录待 MCP 同步列表）.

    Args:
        skip_cloud_sync: 为 True 时，不直接调用 _mx_add，而是将待同步项记录到 results["pending_cloud_sync"]，
                      供上层 Agent 统一通过 MCP 工具同步到云端。
    """
    print("\n" + "=" * 70)
    print(f"📂 阶段 2/3: 更新 '{group_name}' 本地分组")
    print("=" * 70)

    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"❌ 分组 '{group_name}' 不存在，请先创建")
        return {"added": [], "failed": [], "skipped": [], "pending_cloud_sync": []}

    existing = set(data["groups"][group_name].get("stocks", []))
    results = {"added": [], "failed": [], "skipped": [], "details": [], "pending_cloud_sync": []}

    for stock in stocks:
        code = stock.get("code", "")
        name = stock.get("name", "")
        rating = stock.get("rating", "")

        if not code or len(code) != 6:
            print(f"   ⚠️  跳过无效代码: {code}")
            results["skipped"].append(code)
            continue

        ticker = _normalize_ticker(code)

        if ticker in existing:
            print(f"   ⏭️  {name} ({ticker}) 已在分组中")
            results["skipped"].append(ticker)
            results["details"].append({"ticker": ticker, "name": name, "status": "skipped", "reason": "已在分组中"})
            continue

        # 本地分组更新
        existing.add(ticker)
        results["added"].append(ticker)
        results["details"].append({"ticker": ticker, "name": name, "status": "added", "rating": rating})
        print(f"   ➕ {name} ({ticker}) [评级:{rating}] 已加入本地分组")

        # 记录待 MCP 同步项
        if skip_cloud_sync:
            results["pending_cloud_sync"].append({
                "ticker": ticker,
                "name": name,
                "rating": rating,
                "group": group_name,
            })
        else:
            # 保留直接 HTTP 同步能力（退适模式）
            print(f"   ☁️  {name} ({ticker}) → 直接同步到云端...")
            if _mx_add(ticker, group_name):
                print(f"   ✅ {name} 云端同步成功")
            else:
                results["failed"].append(ticker)
                results["details"][-1]["status"] = "failed"
                print(f"   ❌ {name} 云端同步失败")

    data["groups"][group_name]["stocks"] = sorted(list(existing))
    _save_groups(data)

    # 保存待 MCP 同步列表到 JSON，供上层 Agent / cron 读取
    if results["pending_cloud_sync"]:
        pending_path = RESULTS_DIR / "pending_cloud_sync.json"
        try:
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump({
                    "group": group_name,
                    "timestamp": datetime.now().isoformat(),
                    "stocks": results["pending_cloud_sync"],
                }, f, ensure_ascii=False, indent=2)
            print(f"\n   📝 待 MCP 同步列表已写入: {pending_path} ({len(results['pending_cloud_sync'])} 只)")
        except Exception as e:
            print(f"\n   ⚠️  写入 pending_cloud_sync.json 失败: {e}")

    return results


def _generate_report(scan_results: List[Dict], add_results: Dict, elapsed: float) -> str:
    """生成操作报告."""
    lines = []
    lines.append("\n" + "=" * 70)
    lines.append(" 📊 每日扫描 + 分组更新报告")
    lines.append("=" * 70)
    lines.append(f" 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f" 耗时: {elapsed / 60:.1f} 分钟")
    lines.append(f" 分组: {TARGET_GROUP}")
    lines.append("")

    # 扫描结果
    lines.append(f"📊 扫描发现 {len(scan_results)} 只值得关注的股票:")
    for s in scan_results:
        lines.append(f"   • {s.get('name', 'N/A')} ({s.get('code', '')}) → 评级: {s.get('rating', 'N/A')}")
    lines.append("")

    # 添加结果
    added = add_results.get("added", [])
    skipped = add_results.get("skipped", [])
    failed = add_results.get("failed", [])
    pending = add_results.get("pending_cloud_sync", [])

    lines.append("📂 分组更新结果:")
    lines.append(f"   ✅ 本地添加: {len(added)} 只")
    if added:
        for d in add_results.get("details", []):
            if d["status"] == "added":
                lines.append(f"      + {d['name']} ({d['ticker']}) [{d['rating']}]")

    if pending:
        lines.append(f"   ☁️  待 MCP 云端同步: {len(pending)} 只")
        for p in pending:
            lines.append(f"      → {p['name']} ({p['ticker']}) [{p['rating']}]")

    lines.append(f"   ⏭️  已存在跳过: {len(skipped)} 只")
    if failed:
        lines.append(f"   ❌ 直接同步失败: {len(failed)} 只")
        for d in add_results.get("details", []):
            if d["status"] == "failed":
                lines.append(f"      ✗ {d['name']} ({d['ticker']})")

    lines.append("")

    # 当前分组状态
    data = _load_groups()
    group = data.get("groups", {}).get(TARGET_GROUP, {})
    all_stocks = group.get("stocks", [])
    lines.append(f"📁 当前 '{TARGET_GROUP}' 分组共 {len(all_stocks)} 只股票:")
    for i, t in enumerate(all_stocks, 1):
        lines.append(f"   {i}. {t}")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日扫描+自动添加好股到分组")
    parser.add_argument("--group", default=TARGET_GROUP, help="目标分组名称")
    parser.add_argument("--strategy", choices=["conservative", "balanced", "aggressive"],
                        default="balanced", help="二级评分档位 (default: balanced)")
    parser.add_argument("--top-n", type=int, default=5, help="深度分析数量 (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际添加")
    parser.add_argument("--skip-cloud-sync", action="store_true", default=True,
                        help="不直接调用 _mx_add，而是将待同步项记录到 pending_cloud_sync.json，供上层 Agent 统一通过 MCP 同步到云端 (default: True)")
    parser.add_argument("--direct-cloud-sync", action="store_true", default=False,
                        help="直接调用 _mx_add 进行云端同步（退适模式，不推荐）")
    args = parser.parse_args()

    group_name = args.group
    strategy = args.strategy
    top_n = args.top_n
    # 默认走 MCP 同步模式，除非显式指定 --direct-cloud-sync
    skip_cloud_sync = args.skip_cloud_sync and not args.direct_cloud_sync
    start_time = time.time()

    print("\n" + "🚀" * 35)
    print("  每日市场扫描 + 自动分组")
    print(f"  档位: {strategy} | Top N: {top_n} | 分组: {group_name}")
    sync_mode = "MCP 同步模式（待上层 Agent 同步）" if skip_cloud_sync else "直接云端同步模式"
    print(f"  同步: {sync_mode}")
    print("  " + "=" * 66)

    # Step 1: 运行扫描
    scan_results = _run_scan(strategy=strategy, top_n=top_n)
    # 最终防线: 如果 _run_scan 返回空，再试一次 JSON fallback
    if not scan_results:
        print("\n⚠️  扫描结果为空，尝试最终 JSON fallback...")
        scan_results = _parse_from_json_candidates()
        if scan_results:
            print(f"   ✅ 最终 fallback 找到 {len(scan_results)} 只 BUY/OVERWEIGHT")

    if not scan_results:
        print("\n📭 今日未发现值得关注的股票（或扫描超时/失败）")
        # 即使没有结果也保存通知，避免 cron 无法提取 message
        _save_notification([], {"added": [], "failed": [], "skipped": [], "details": [], "pending_cloud_sync": []}, group_name, strategy)
        return

    # Step 2: 添加到分组
    if args.dry_run:
        print(f"\n🧪 试运行模式，以下股票将被添加到 '{group_name}':")
        for s in scan_results:
            print(f"   • {s.get('name')} ({s.get('code')}) → {s.get('rating')}")
        add_results = {"added": [], "failed": [], "skipped": [], "details": [], "pending_cloud_sync": []}
    else:
        add_results = _add_to_group(scan_results, group_name, skip_cloud_sync=skip_cloud_sync)

    # Step 3: 生成报告
    elapsed = time.time() - start_time
    report = _generate_report(scan_results, add_results, elapsed)
    print(report)

    # 保存报告
    report_path = RESULTS_DIR / f"daily_scan_add_{strategy}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n📄 报告已保存: {report_path}")

    # 保存通知摘要（供 cron 发送 QQ 消息用）
    _save_notification(scan_results, add_results, group_name, strategy)

    # 如果有待 MCP 同步项，提示上层 Agent
    pending = add_results.get("pending_cloud_sync", [])
    if pending:
        print("\n⚠️  请求上层 Agent 执行 MCP 云端同步:")
        for p in pending:
            print(f"   • 添加 {p['name']} ({p['ticker']}) 到 '{p['group']}' 分组")
        print(f"\n   待同步列表已保存到: {RESULTS_DIR}/pending_cloud_sync.json")
        print("   请运行: mcp_mx_skills_mx_zixuan_query('添加 <ticker> 到 <group> 分组') 完成同步")


def _save_notification(scan_results, add_results, group_name, strategy="balanced"):
    """保存精简通知摘要为 JSON，供 cron 定时任务发送 QQ 消息.

    无论是否有 BUY/OVERWEIGHT 入选，都显示完整的量化候选列表，
    让用户清楚知道"扫描了哪些、为什么没入选"。
    """
    added = add_results.get("added", [])
    failed = add_results.get("failed", [])
    skipped = add_results.get("skipped", [])

    # 构建精简摘要
    lines = []
    lines.append(f"📊 每日市场扫描 | {datetime.now().strftime('%m-%d %H:%M')} | 档位: {strategy}")
    lines.append("")

    # ── 阶段 1: 入选股票（BUY / OVERWEIGHT）──
    if scan_results:
        lines.append(f"🎯 发现 {len(scan_results)} 只值得关注的股票:")
        for s in scan_results:
            name = s.get("name", "")
            code = s.get("code", "")
            rating = s.get("rating", "")
            reason = s.get("reason", "")
            lines.append(f"  • {name}({code}) → {rating}")
            if reason:
                lines.append(f"    理由: {reason}")
        lines.append("")

        if added:
            lines.append(f"✅ 已添加到 '{group_name}': {len(added)} 只")
            for d in add_results.get("details", []):
                if d["status"] == "added":
                    lines.append(f"  + {d['name']}({d['ticker']})")
        if skipped:
            lines.append(f"⏭️  已存在跳过: {len(skipped)} 只")
        if failed:
            lines.append(f"❌ 同步失败: {len(failed)} 只")
        lines.append("")
    else:
        lines.append("📭 今日未发现值得关注的股票（BUY / OVERWEIGHT）")
        lines.append("")

    # ── 阶段 2: 量化通过的全部候选（始终显示）──
    candidates = []
    try:
        latest_json = RESULTS_DIR / "scan_candidates_latest.json"
        if latest_json.exists():
            with open(latest_json, "r", encoding="utf-8") as f:
                candidates = json.load(f)
    except Exception:
        pass

    # 从汇总报告中提取统计
    summary_stats = {}
    try:
        summary_files = sorted(RESULTS_DIR.glob("scan_summary_*.txt"), reverse=True)
        if summary_files:
            summary_text = summary_files[0].read_text(encoding="utf-8")
            for sl in summary_text.splitlines():
                if "分析股票数:" in sl:
                    summary_stats["分析数"] = sl.split("分析股票数:")[-1].strip()
                elif "评级分布:" in sl or "HOLD" in sl or "UNDERWEIGHT" in sl:
                    if "评级分布" not in summary_stats:
                        summary_stats["评级分布"] = []
                    if any(r in sl for r in ["HOLD", "UNDERWEIGHT", "BUY", "OVERWEIGHT", "SELL"]):
                        summary_stats["评级分布"].append(sl.strip())
    except Exception:
        pass

    if candidates:
        lines.append("📊 扫描过程:")
        lines.append(f"  • 量化筛选通过: {len(candidates)} 只")
        if summary_stats.get("分析数"):
            lines.append(f"  • LLM 分析: {summary_stats['分析数']} 只")
        lines.append("")

        # 分离入选 vs 未入选
        buy_codes = {s.get("code", "") for s in scan_results}
        not_selected = [c for c in candidates if c.get("code", "") not in buy_codes]

        if not_selected:
            lines.append("📈 量化通过但 LLM 未入选的标的：")
            for i, c in enumerate(not_selected[:10], 1):
                name = c.get("name", "N/A")
                code = c.get("code", "")
                rating = "N/A"
                reason = c.get("analysis_reason", "")
                if reason:
                    for r in ["BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"]:
                        if r in reason.upper():
                            rating = r
                            break
                score = c.get("score", 0)
                inflow = c.get("main_inflow_million", 0)
                lines.append(f"  {i}. {name}({code}) → {rating} | 综合分:{score:.1f} | 主力流入:{inflow:+.0f}万")
                if reason and len(reason) > 10:
                    brief = reason.replace("\n", " ").strip()[:80]
                    lines.append(f"     理由: {brief}...")
            lines.append("")
            lines.append("ℹ️ 以上标的量化层面通过筛选，但 LLM 认为风险收益比不优或时机不成熟，未达到 BUY/OVERWEIGHT 标准。")
        else:
            lines.append("✅ 所有量化通过的标的均已入选。")
        lines.append("")
    else:
        lines.append("📋 量化筛选未能通过足够多的候选股，或扫描过程异常。")
        lines.append("")

    # 当前分组总数
    data = _load_groups()
    group = data.get("groups", {}).get(group_name, {})
    total = len(group.get("stocks", []))
    lines.append(f"📁 当前'{group_name}'共 {total} 只")

    summary_text = "\n".join(lines)

    # 保存简化 JSON
    notification = {
        "type": "daily_scan",
        "timestamp": datetime.now().isoformat(),
        "strategy": strategy,
        "group": group_name,
        "found": len(scan_results),
        "added": len(added),
        "failed": len(failed),
        "skipped": len(skipped),
        "total_in_group": total,
        "stocks": [
            {"name": s.get("name"), "code": s.get("code"), "rating": s.get("rating"), "reason": s.get("reason", "")}
            for s in scan_results
        ],
        "message": summary_text,
    }

    notify_path = RESULTS_DIR / "notification_latest.json"
    notify_path.write_text(json.dumps(notification, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📤 通知摘要已保存: {notify_path}")


if __name__ == "__main__":
    main()
