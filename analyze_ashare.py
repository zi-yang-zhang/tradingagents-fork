#!/usr/bin/env python3
"""A-Share Stock Analyzer — TradingAgents 主分析入口.
    数据源: stock_data_hub (AkShare + westock-data)
    输出语言: Chinese

"运行模式:
    - deep:    深度分析 (5轮辩论 + 5轮风险讨论) — 用于重要决策
    - medium:  中等分析 (3轮辩论 + 3轮风险讨论) — 平衡深度与速度，**默认**
    - shallow: 浅度分析 (1轮辩论 + 1轮风险讨论) — 快速筛选

用法:
    cd ~/TradingAgents && source .venv/bin/activate
    python analyze_ashare.py

配置（修改脚本底部 __main__ 区域的以下变量）:
    USER_INPUT = "中天科技"              # 股票名称/代码
    ANALYSIS_MODE = "deep"               # "deep" 或 "shallow"
    ANALYSIS_ANALYSTS = "all"            # "all" 或指定分析师列表

LLM 配置（修改脚本顶部以下常量）:
    LLM_PROVIDER = "deepseek"            # LLM 供应商
    LLM_BACKEND_URL = "https://api.deepseek.com"
    DEEP_THINK_LLM = "deepseek-v4-pro"   # 深度思考模型
    QUICK_THINK_LLM = "deepseek-v4-flash" # 快速思考模型
    OUTPUT_LANGUAGE = "Chinese"          # 输出语言

输出:
    - 实时打印每个节点的执行进度
    - 中间结果每 10 chunk 保存至 stock_analysis_result/
    - 最终报告保存至 stock_analysis_result/{TICKER}_{DATE}_{MODE}_report.json

完整运行流程:
    参见 _run_analysis() 函数内注释。
"""

import os

# 默认开启妙想(MX)优先，避免回退到 westock-data (npx 在 cron 等环境可能缺失/慢)
os.environ.setdefault("STOCK_DATA_HUB_MX_FIRST", "1")

# 从 ~/.bashrc 加载 MX_APIKEY（scan_ashare 也会这样做）
_MX_KEY_LOADED = False
def _load_mx_apikey():
    global _MX_KEY_LOADED
    if _MX_KEY_LOADED:
        return
    bashrc = os.path.expanduser("~/.bashrc")
    if os.path.exists(bashrc):
        try:
            with open(bashrc, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export ") and "MX_APIKEY" in line:
                        parts = line[7:].split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            os.environ.setdefault("MX_APIKEY", val)
        except Exception:
            pass
    _MX_KEY_LOADED = True

_load_mx_apikey()

import pandas as pd
from datetime import datetime
from pathlib import Path

# API key loaded from environment; set DEEPSEEK_API_KEY in .env or shell

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
import time


# =====================================================================
# LLM 配置区 — 根据需要修改以下常量
# =====================================================================
LLM_PROVIDER = "deepseek"                           # 支持: deepseek, openai, anthropic, qwen, glm, ...
LLM_BACKEND_URL = "https://api.deepseek.com"        # 对应 provider 的 API 基础地址
DEEP_THINK_LLM = "deepseek-v4-pro"                  # 深度思考模型（Research Manager, Portfolio Manager 等）
QUICK_THINK_LLM = "deepseek-v4-flash"               # 快速思考模型（Market Analyst, Trader, Debators 等）
OUTPUT_LANGUAGE = "Chinese"                         # 报告输出语言: "Chinese" | "English" | "Japanese"

# 数据源配置 — 默认使用 stock_data_hub，可改为 "yfinance", "alpha_vantage", ...
DATA_VENDOR = "stock_data_hub"

# =====================================================================
# Ticker resolution: name/code → standard ticker format
# =====================================================================

def _is_valid_ticker_format(s: str) -> bool:
    """Check if string is already a standard ticker format."""
    s = s.strip()
    if s.endswith((".SZ", ".SS", ".BJ", ".sz", ".ss", ".bj")):
        code = s.split(".")[0]
        return code.isdigit() and len(code) == 6
    return False


def _is_6digit_code(s: str) -> bool:
    """Check if string is a 6-digit code (no suffix)."""
    s = s.strip()
    return s.isdigit() and len(s) == 6


def resolve_ticker(input_str: str) -> tuple[str, str]:
    """Resolve user input to standard ticker format.

    Args:
        input_str: User input - can be:
            - Full ticker: "002475.SZ", "600519.SS"
            - Pure code: "002475", "600519" (auto-detect exchange)
            - Chinese name: "立讯精密", "茅台"
            - Partial name: "立讯"

    Returns:
        (ticker, company_name) where ticker is in XXXXXX.SZ/SS/BJ format
    """
    input_str = input_str.strip()

    # Case 1: Already full ticker format
    if _is_valid_ticker_format(input_str):
        ticker = input_str.upper()
        # Try to get name
        name = _get_company_name(ticker)
        return ticker, name or ticker

    # Case 2: Pure 6-digit code
    if _is_6digit_code(input_str):
        code = input_str
        # SH: starts with 6 or 9 (main board, STAR board)
        # SZ: starts with 0, 2, or 3 (main, SME, ChiNext)
        # BJ: starts with 8 or 4 (Beijing Stock Exchange)
        if code.startswith(("6", "9")):
            suffix = ".SS"
        elif code.startswith(("8", "4")):
            suffix = ".BJ"
        else:
            suffix = ".SZ"
        ticker = code + suffix
        name = _get_company_name(ticker)
        return ticker, name or ticker

    # Case 3: Name (Chinese or partial) - search via AkShare
    name = input_str
    ticker = _search_ticker_by_name(name)
    if ticker:
        matched_name = _get_company_name(ticker) or name
        print(f'[信息] "名称解析" "{name}" → {ticker} ({matched_name})')
        return ticker, matched_name

    # Fallback: return as-is (will likely fail downstream)
    print(f'[错误] 无法解析 "{input_str}" 为有效股票代码，请使用 6位数字代码（如 000858）或完整ticker（如 000858.SZ）重试。')
    raise SystemExit(1)


def _get_company_name(ticker: str) -> str | None:
    """Get company name for a ticker from cached stock list."""
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        code = ticker.split(".")[0]
        row = df[df["code"] == code]
        if not row.empty:
            return row.iloc[0]["name"]
    except Exception:
        pass
    return None


def _search_ticker_by_name(name: str) -> str | None:
    """Search ticker by company name (Chinese or partial)."""
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()

        # Exact match
        exact = df[df["name"] == name]
        if not exact.empty:
            code = exact.iloc[0]["code"]
            return _code_to_ticker(code)

        # Partial match (contains)
        partial = df[df["name"].str.contains(name, na=False, regex=False)]
        if len(partial) == 1:
            code = partial.iloc[0]["code"]
            return _code_to_ticker(code)
        elif len(partial) > 1:
            print("[警告] 多个匹配结果，请更精确地指定：")
            for _, row in partial.head(5).iterrows():
                t = _code_to_ticker(row["code"])
                print(f"    {row['code']} ({t}) - {row['name']}")
            return None

        # Fuzzy: try matching from the start
        fuzzy = df[df["name"].str.startswith(name, na=False)]
        if len(fuzzy) == 1:
            code = fuzzy.iloc[0]["code"]
            return _code_to_ticker(code)
        elif len(fuzzy) > 1:
            print("[警告] 多个匹配结果，请更精确地指定：")
            for _, row in fuzzy.head(5).iterrows():
                t = _code_to_ticker(row["code"])
                print(f"    {row['code']} ({t}) - {row['name']}")
            return None

    except Exception as e:
        print(f"[错误] 搜索股票失败: {e}")

    return None


def _code_to_ticker(code: str) -> str:
    """Convert 6-digit code to ticker with suffix."""
    if code.startswith(("6", "9")):
        return code + ".SS"
    elif code.startswith(("8", "4")):
        return code + ".BJ"
    else:
        return code + ".SZ"


# =====================================================================
# Analysis runner — 完整分析流程
def _run_analysis(
    user_input: str,
    mode: str = "medium",
    analysts: str = "all",
    holding_info: dict | None = None,
) -> None:
    """执行 A 股完整分析流程.

    参数:
        user_input: 股票名称/代码（"立讯精密", "002475", "002475.SZ", "茅台"）
        mode: 分析模式
            "deep"    → 5轮辩论 + 5轮风险（约 10-20 分钟）
            "medium"  → 3轮辩论 + 3轮风险（约 5-10 分钟，**默认**）
            "shallow" → 1轮辩论 + 1轮风险（约 2-3 分钟）
        analysts: 分析师团队
            "all"     → 全部 5 个：market + fundamentals + news + social + china_market
            列表    → 自定义，如 ["market", "news"]
        holding_info: 持仓信息（可选，默认 None）
            {"shares": 1000, "cost_price": 35.50, "weight": 0.15}
            设置后 Portfolio Manager 会在决策时考虑持仓状况：浮动盈亏、组合占比、是
            否该减仓/清仓/追加。

    完整流程（18 个步骤）：

    Step 1 — 股票解析：中文名称/代码 → 标准 ticker 格式
    Step 2 — 检测最新数据日期
    Step 3 — 配置组装：LLM + 数据源 + 语言
    Step 4 — 初始化 TradingAgentsGraph（根据分析师动态构建）
    Step 5 — 触发历史决策反思（reflection）
    Step 6 — 获取历史上下文（past_context）
    Step 7 — 持仓信息注入（可选）
    Step 8 — 创建初始状态 + checkpoint 配置
    Step 9 — 流式执行开始

    分析师节点（顺序执行，取决于 selected_analysts）：
        Step 10  Market Analyst         — K线 + 技术指标 → 技术报告
        Step 11  Fundamentals Analyst   — 财务三表 → 基本面报告
        Step 12  News Analyst           — 东方财富新闻 → 新闻报告
        Step 13  Sentiment Analyst      — 市场情绪 → 情绪报告
        Step 14  China Market Analyst   — A股资金流（北向/融资融券/主力资金） → 流动性报告

    研究阶段：
        Step 15 Bull Researcher      — 看涨论据
        Step 16 Bear Researcher      — 看跌论据
        Step 17 多空辩论循环    — Bull↔Bear 循环（mode 决定轮数）
        Step 18 Research Manager     — 汇总 → 投资建议书
        Step 19 Trader               — 制定交易计划

    风险阶段：
        Step 20 风险讨论循环    — 激进↔保守↔中立（mode 决定轮数）
        Step 21 Portfolio Manager    — 最终决策 BUY/HOLD/SELL

    保存:
        框架标准报告:  stock_analysis_result/{TICKER}/TradingAgentsStrategy_logs/
        兼容报告:       stock_analysis_result/{TICKER}_{DATE}_{MODE}_report.json
        决策记忆库:      stock_analysis_result/.memory/trading_memory.md
        摘要:            stock_analysis_result/.summaries/{TICKER}.jsonl
        中间进度:        每 10 chunk 保存 temp 文件
    """
    # 解析 mode 参数
    mode = mode.lower().strip()
    if mode == "deep":
        debate_rounds, risk_rounds = 5, 5
        mode_label = "深度"
    elif mode == "medium":
        debate_rounds, risk_rounds = 3, 3
        mode_label = "中等"
    elif mode == "shallow":
        debate_rounds, risk_rounds = 1, 1
        mode_label = "浅度"
    else:
        print(f"[警告] 未知模式 '{mode}'，使用默认中等模式")
        debate_rounds, risk_rounds = 3, 3
        mode_label = "中等(默认)"

    # 解析 analysts 参数
    if analysts == "all":
        selected_analysts = ["market", "fundamentals", "news", "social", "china_market"]
    elif isinstance(analysts, str):
        # 可能是逗号分隔的字符串
        selected_analysts = [a.strip() for a in analysts.split(",")]
    else:
        selected_analysts = list(analysts)

    print(f"[信息] 分析模式: {mode_label}（辩论 {debate_rounds} 轮，风险 {risk_rounds} 轮）")
    print(f"[信息] 分析师团队: {', '.join(selected_analysts)}")

    # Step 1-2: 股票解析 + 检测日期
    ticker, company_name = resolve_ticker(user_input)

    # 直接用 AkShare 获取最近交易日（最可靠、最快）
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        import akshare as ak
        trade_dates = ak.tool_trade_date_hist_sina()
        trade_dates["trade_date"] = pd.to_datetime(trade_dates["trade_date"])
        latest_trade = trade_dates[trade_dates["trade_date"] <= pd.Timestamp.now()].iloc[-1]
        analysis_date = latest_trade["trade_date"].strftime("%Y-%m-%d")
        print(f"[INFO] 最近交易日: {analysis_date}")
    except Exception:
        analysis_date = today_str
        print(f"[WARN] 无法获取交易日，使用今日: {analysis_date}")

    # 校验: 如果检测到的日期远早于今天，打印明显警告
    if analysis_date != today_str:
        print(f"\n[⚠️ 重要提醒] 检测到的分析日期为 {analysis_date}，而今天是 {today_str}。")
        print("          请确认该日期是否为您期望的分析日期。如果不是，请检查数据源或手动指定日期。\n")

    # Step 3: 配置
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = LLM_PROVIDER
    config["backend_url"] = LLM_BACKEND_URL
    config["deep_think_llm"] = DEEP_THINK_LLM
    config["quick_think_llm"] = QUICK_THINK_LLM
    config["output_language"] = OUTPUT_LANGUAGE
    config["max_debate_rounds"] = debate_rounds
    config["max_risk_discuss_rounds"] = risk_rounds
    config["data_vendors"] = {
        "core_stock_apis": DATA_VENDOR,
        "technical_indicators": DATA_VENDOR,
        "fundamental_data": DATA_VENDOR,
        "news_data": DATA_VENDOR,
    }

    # ─── 标准化框架配置 ───
    # 启用 checkpoint/resume，崩溃后可从上次节点恢复
    config["checkpoint_enabled"] = True
    # 将框架目录指向项目内 stock_analysis_result/，便于管理
    config["results_dir"] = os.path.join(os.getcwd(), "stock_analysis_result")
    config["data_cache_dir"] = os.path.join(os.getcwd(), "stock_analysis_result", ".cache")
    config["memory_log_path"] = os.path.join(os.getcwd(), "stock_analysis_result", ".memory", "trading_memory.md")
    # A 股基准指数映射（供 reflection 计算 alpha 用）
    config["benchmark_map"] = {
        **DEFAULT_CONFIG.get("benchmark_map", {}),
        ".SS": "000300.SS",   # 沪深300
        ".SZ": "399001.SZ",   # 深成指
        ".BJ": "899050.BJ",   # 北证50
    }

    print("=" * 70)
    print(f"TradingAgents - {company_name} ({ticker}) {mode_label}分析")
    print("=" * 70)
    print(f"分析日期: {analysis_date}")
    print(f"分析师团队: {', '.join(selected_analysts)}")
    print(f"研究深度: {mode_label} (辩论{debate_rounds}轮 + 风险{risk_rounds}轮)")
    print(f"LLM Provider: {LLM_PROVIDER}")
    print(f"Deep Think: {DEEP_THINK_LLM}")
    print(f"Quick Think: {QUICK_THINK_LLM}")
    print(f"数据供应商: {DATA_VENDOR}")
    print(f"输出语言: {OUTPUT_LANGUAGE}")
    print("=" * 70)

    # Step 4: 初始化 Graph
    graph = TradingAgentsGraph(
        selected_analysts=selected_analysts,
        config=config,
        debug=False,
    )

    # Step A: 解析历史 pending entries（如果之前分析过此股票，触发 reflection）
    print("[信息] 检查历史决策记录...")
    graph._resolve_pending_entries(ticker)

    # Step B: 获取历史反思上下文
    use_enhanced = os.environ.get("TRADINGAGENTS_ENHANCED_REFLECTION", "true").lower() == "true"
    if use_enhanced:
        try:
            from scripts.enhanced_past_context import build_enhanced_past_context
            memory_path = config.get("memory_log_path", "stock_analysis_result/.memory/trading_memory.md")
            past_context = build_enhanced_past_context(
                ticker, memory_path,
                min_score=0.0,
                max_same=5,
                max_cross=3,
                include_accuracy_summary=True,
            )
            if past_context and past_context.strip():
                print(f"[信息] 已加载增强版历史反思上下文 ({len(past_context)} 字符)")
            else:
                past_context = ""
        except Exception as e:
            print(f"[警告] 增强版 past_context 加载失败: {e}，回退到默认模式")
            past_context = graph.memory_log.get_past_context(ticker) or ""
    else:
        past_context = graph.memory_log.get_past_context(ticker)
        if past_context and past_context.strip():
            print(f"[信息] 已加载 {len(past_context)} 字符的历史反思上下文")
        else:
            past_context = ""

    # 增强版开关提示
    if use_enhanced:
        print("[信息] 增强 reflection 已启用 (质量评分 + 标签 + 分析师准确率)")

    # ─── 持仓信息注入 ───
    if holding_info and isinstance(holding_info, dict):
        try:
            from tradingagents.dataflows.stock_data_hub import get_stock
            from io import StringIO
            stock_csv = get_stock(ticker, analysis_date, analysis_date)
            df = pd.read_csv(StringIO(stock_csv), comment='#')
            if len(df) > 0 and 'Close' in df.columns:
                current_price = float(df['Close'].iloc[-1])
            else:
                current_price = None
        except Exception:
            current_price = None

        shares = holding_info.get("shares", 0)
        cost = holding_info.get("cost_price", 0.0)
        weight = holding_info.get("weight", 0.0)
        display_price = current_price if current_price is not None else cost
        pnl = (current_price - cost) * shares if current_price else 0
        pnl_pct = ((current_price - cost) / cost * 100) if (current_price and cost) else 0
        mkt_value = (current_price * shares) if current_price else (cost * shares)

        position_context = f"""\n［用户当前持仓信息］\n- 持有标的：{ticker}\n- 持仓数量：{shares} 股\n- 成本价：{cost:.2f} 元\n- 当前价：{display_price:.2f} 元 \uff08{"实时获取" if current_price else "未能获取，以成本价替代"}）\n- 浮动盈亏：{pnl:,.0f} 元 \uff08{pnl_pct:+.1f}%）\n- 市值：{mkt_value:,.0f} 元\n- 组合占比：{weight*100:.1f}%\n\n［持仓-aware 决策要求］\n1. 若建议 Sell 或 Underweight，请明确是全部清仓还是部分减仓，并给出具体股数/金额。\n2. 若建议 Buy 或 Overweight，请说明是否适合在已有底仓基础上追加或降低成本。\n3. 若建议 Hold，请给出持有期限建议以及止盈/止损参考价。\n4. 如果当前浮动盈亏达到一定比例（如 +20% 或 -10%），请专门评估是否应该锁定利润/截止亏损。\n"""
        past_context = past_context + position_context
        print(f"[信息] 已注入持仓信息: {shares}股, 成本{cost:.2f}, 浮盈{pnl_pct:+.1f}%")

    # Step D: 创建初始状态（传入 past_context，含历史反思 + 持仓信息）
    init_state = graph.propagator.create_initial_state(
        ticker, analysis_date, past_context=past_context if past_context.strip() else None
    )

    # ▶️ 关键：设置 graph.ticker ，供 _log_state() 使用
    graph.ticker = ticker

    args = graph.propagator.get_graph_args()

    # ─── Checkpoint 处理 ───
    checkpointer_ctx = None
    if config.get("checkpoint_enabled"):
        from tradingagents.graph.checkpointer import get_checkpointer, thread_id, checkpoint_step
        checkpointer_ctx = get_checkpointer(config["data_cache_dir"], ticker)
        saver = checkpointer_ctx.__enter__()
        graph.graph = graph.workflow.compile(checkpointer=saver)
        tid = thread_id(ticker, str(analysis_date))
        args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid
        step = checkpoint_step(config["data_cache_dir"], ticker, str(analysis_date))
        if step is not None:
            print(f"[信息] 从 checkpoint step {step} 恢复...")
        else:
            print("[信息] 全新运行，checkpoint 已启用")

    # Step E+: 流式执行
    start = time.time()
    chunk_count = 0
    final_state = None

    try:
        for chunk in graph.graph.stream(init_state, **args):
            chunk_count += 1
            final_state = chunk
            elapsed = time.time() - start
            node_name = list(chunk.keys())[0] if chunk else "unknown"
            print(f"[{elapsed:7.1f}s] Chunk {chunk_count:2d}: {node_name}")

            for key in ["market_report", "fundamentals_report", "news_report",
                        "investment_plan", "trader_investment_plan", "final_trade_decision"]:
                if key in chunk and chunk[key]:
                    content = str(chunk[key])[:60].replace('\n', ' ')
                    print(f"           → {key}: {content}...")

            # 中间保存
            if chunk_count % 10 == 0 and final_state:
                import json
                from pathlib import Path
                results_dir = Path("stock_analysis_result")
                results_dir.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%H%M%S")
                temp_file = results_dir / f"{ticker}_{analysis_date}_{timestamp}_temp_{chunk_count}.json"
                save_data = {k: v for k, v in final_state.items()
                             if isinstance(v, (str, int, float, bool, list, dict))}
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
                print(f"           💾 中间结果已保存: {temp_file}")

        # 总结
        elapsed = time.time() - start
        print(f"\n{'=' * 70}")
        print(f"✅ 分析完成！{chunk_count} 个步骤，耗时 {elapsed:.1f} 秒 ({elapsed/60:.1f} 分钟)")

        if final_state:
            print("\n" + "=" * 70)
            print("📊 最终交易决策")
            print("=" * 70)

            if "final_trade_decision" in final_state and final_state["final_trade_decision"]:
                print(final_state["final_trade_decision"])

            # Step F: 保存到框架标准目录
            graph._log_state(analysis_date, final_state)
            print(f"\n💾 框架标准报告已保存: {config['results_dir']}/{ticker}/TradingAgentsStrategy_logs/")

            # Step G: 保存到 memory log（供下次 reflection 使用）
            graph.memory_log.store_decision(
                ticker=ticker,
                trade_date=analysis_date,
                final_trade_decision=final_state["final_trade_decision"],
            )
            print(f"📝 决策已记录到 memory log: {config['memory_log_path']}")

            # Step H: 兼容保存到 stock_analysis_result/（供 scan_ashare.py 读取）
            import json
            from pathlib import Path
            results_dir = Path("stock_analysis_result")
            results_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%H%M%S")
            mode_label = mode if 'mode' in locals() else "medium"
            report_file = results_dir / f"{ticker}_{analysis_date}_{timestamp}_{mode_label}_report.json"

            save_data = {k: v for k, v in final_state.items()
                         if isinstance(v, (str, int, float, bool, list, dict))}
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)

            print(f"💾 兼容报告已保存: {report_file}")

    finally:
        if checkpointer_ctx is not None:
            checkpointer_ctx.__exit__(None, None, None)
            # 成功完成后清理 checkpoint
            from tradingagents.graph.checkpointer import clear_checkpoint
            clear_checkpoint(config["data_cache_dir"], ticker, str(analysis_date))
            print("[信息] Checkpoint 已清理")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="A-Share Stock Analyzer")
    parser.add_argument("--stock", default="上海电气", help="股票名称/代码 (如: 中天科技, 600522, 600522.SS)")
    parser.add_argument("--mode", choices=["deep", "medium", "shallow"], default="medium", help="分析模式")
    parser.add_argument("--analysts", default="all", help="分析师团队 (默认: all)")
    parser.add_argument("--holding", type=str, default=None, help="持仓信息 JSON (如: '{\"shares\":1000,\"cost_price\":35.5,\"weight\":0.15}')")
    parser.add_argument("--no-auto-holding", action="store_true", help="禁用自动从 ~/.hermes/stock_holdings.json 读取持仓 (默认自动启用)")
    args = parser.parse_args()

    holding_info = None
    if args.holding:
        import json
        holding_info = json.loads(args.holding)
    elif not args.no_auto_holding:
        # 默认行为: 自动从 stock_holdings.json 读取持仓
        holdings_path = Path.home() / ".hermes" / "stock_holdings.json"
        if holdings_path.exists():
            try:
                import json
                data = json.loads(holdings_path.read_text(encoding="utf-8"))
                # 先解析目标股票的 ticker
                target_ticker, _ = resolve_ticker(args.stock)
                # 在 holdings 中查找匹配
                for h in data.get("holdings", []):
                    if h.get("ticker") == target_ticker:
                        holding_info = {
                            "shares": h.get("shares", 0),
                            "cost_price": h.get("cost_price", 0.0),
                            "weight": h.get("weight", 0.0),
                        }
                        print(f"[信息] 自动加载持仓: {h.get('name')} | {holding_info['shares']}股, 成本{holding_info['cost_price']}, 占比{holding_info['weight']*100:.2f}%")
                        break
                if not holding_info:
                    print(f"[信息] 未在持仓中找到 {target_ticker}，将进行无持仓分析")
            except Exception as e:
                print(f"[警告] 读取 stock_holdings.json 失败: {e}")
        else:
            print(f"[警告] 未找到持仓文件: {holdings_path}")

    _run_analysis(
        user_input=args.stock,
        mode=args.mode,
        analysts=args.analysts,
        holding_info=holding_info,
    )
