#!/usr/bin/env python3
"""
A-Share Market Scanner — 批量筛选 + 浅度分析流水线.

层级架构:
    Tier 1: 统一初筛（妙想智能选股 + 基础过滤，无 LLM，约 30 秒）
    Tier 2: 按档位评分过滤（保守/平衡/激进）
    Layer 2: LLM 分析（对 Top N 逐一调用 analyze_ashare，每只约 2-3 分钟）

用法:
    cd ~/TradingAgents && source .venv/bin/activate
    python scan_ashare.py [--strategy conservative|balanced|aggressive] [--top-n 10]

参数:
    --strategy  二级评分档位: conservative(保守) / balanced(平衡) / aggressive(激进)
    --top-n     筛选后深度分析的数量
"""

import os

# 默认开启妙想(MX)优先，避免回退到 westock-data (npx 在 cron 等环境可能缺失/慢)
os.environ.setdefault("STOCK_DATA_HUB_MX_FIRST", "1")

import sys
import time
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# =====================================================================
# 持仓自动标注
# =====================================================================
_portfolio_codes: Optional[set] = None

def _load_portfolio_codes() -> set:
    """加载 ~/.hermes/stock_holdings.json，返回持仓股票的纯数字代码集合 (如 {'300014', '600522'})."""
    global _portfolio_codes
    if _portfolio_codes is not None:
        return _portfolio_codes
    holdings_path = Path.home() / ".hermes" / "stock_holdings.json"
    codes = set()
    if holdings_path.exists():
        try:
            data = json.loads(holdings_path.read_text(encoding="utf-8"))
            for h in data.get("holdings", []):
                ticker = h.get("ticker", "")
                if ticker:
                    code = ticker.split(".")[0]
                    if code:
                        codes.add(code)
            print(f"[信息] 已加载持仓: {len(codes)} 只股票")
        except Exception as e:
            print(f"[警告] 读取 stock_holdings.json 失败: {e}")
    else:
        print(f"[信息] 未找到持仓文件: {holdings_path}")
    _portfolio_codes = codes
    return codes

# 确保 TradingAgents 在路径中
sys.path.insert(0, str(Path(__file__).parent))
# 确保 mx-xuangu 在路径中（妙想智能选股）
sys.path.insert(0, "/home/ubuntu/mx-xuangu")

# 加载环境变量（含 MX_APIKEY）
_ENV_LOADED = False
def _load_env_from_bashrc():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        try:
            with open(bashrc, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        parts = line[7:].split("=", 1)
                        if len(parts) == 2:
                            key = parts[0].strip()
                            val = parts[1].strip().strip('"').strip("'")
                            if key and key not in os.environ:
                                os.environ[key] = val
            _ENV_LOADED = True
        except Exception:
            pass
_load_env_from_bashrc()

from analyze_ashare import resolve_ticker

# 导入 stock_data_hub 适配器工具（统一 ticker 格式 + 数据新鲜度验证）
try:
    from tradingagents.dataflows.stock_data_hub import (
        _to_stock_hub_symbol,
        _trade_day_diff,
        _get_last_trade_date,
    )
    _HUB_ADAPTER_AVAILABLE = True
except Exception:
    _HUB_ADAPTER_AVAILABLE = False

# stock_data_hub 查询缓存（避免重复查询）
_hub_cache = {}

def _get_hub_data(code: str):
    """获取股票的资金流向和融资融券数据（带缓存）.
    
    优先使用妙想(MX)查询，失败时回退到 stock_data_hub (westock-data).
    避免在 cron 等环境中因 npx 启动开销导致阻塞.
    """
    if code in _hub_cache:
        return _hub_cache[code]
    
    # ===== 优先尝试妙想(MX) =====
    try:
        import sys
        from pathlib import Path
        home = str(Path.home())
        for p in [f"{home}/mx-data", f"{home}/mx-search"]:
            if p not in sys.path:
                sys.path.insert(0, p)
        import mx_data
        
        client = mx_data.MXData()
        
        # 查询资金流向
        fund_query = f"{code} 主力资金流向 净流入"
        fund_result = client.query(fund_query)
        fund_tables = None
        if fund_result.get("status") == 0:
            fund_tables, _, _, fund_err = mx_data.MXData.parse_result(fund_result)
            if fund_err or not fund_tables:
                fund_tables = None
        
        # 查询融资融券
        margin_query = f"{code} 融资融券余额"
        margin_result = client.query(margin_query)
        margin_tables = None
        if margin_result.get("status") == 0:
            margin_tables, _, _, margin_err = mx_data.MXData.parse_result(margin_result)
            if margin_err or not margin_tables:
                margin_tables = None
        
        if fund_tables or margin_tables:
            import pandas as pd
            fund_df = pd.DataFrame(fund_tables[0].get("rows", [])) if fund_tables else None
            margin_df = pd.DataFrame(margin_tables[0].get("rows", [])) if margin_tables else None
            
            # 标准化妙想列名为 westock-data 格式
            if fund_df is not None and not fund_df.empty:
                # 妙想列名 → westock-data 列名映射
                mx_to_westock = {
                    '主力净额': 'MainNetFlow',
                    '超大单净额': 'JumboNetFlow',
                    '小单净额': 'SmallNetFlow',
                    '大单净额': 'MidNetFlow',
                    '中单净额': 'RetailNetFlow',
                    '3日DDX': 'MainNetFlow3D',
                    '5日DDX': 'MainNetFlow5D',
                    '10日DDX': 'MainNetFlow10D',
                    '当日DDX': 'MainInflowDDX',
                    '当日DDY': 'MainInflowDDY',
                }
                for mx_col, ws_col in mx_to_westock.items():
                    if mx_col in fund_df.columns:
                        fund_df[ws_col] = fund_df[mx_col]
                
                # 将中文金额转换为数值（如 "-4.522亿元" → -452200000）
                def _parse_chinese_amount(val):
                    if pd.isna(val):
                        return 0.0
                    val_str = str(val).strip()
                    if val_str == '':
                        return 0.0
                    # 处理 "亿元", "万元", "元"
                    multiplier = 1
                    if '亿元' in val_str:
                        multiplier = 100000000
                        val_str = val_str.replace('亿元', '')
                    elif '万元' in val_str:
                        multiplier = 10000
                        val_str = val_str.replace('万元', '')
                    elif '元' in val_str:
                        val_str = val_str.replace('元', '')
                    try:
                        return float(val_str) * multiplier
                    except ValueError:
                        return 0.0
                
                # 转换金额列（中文金额 → 数值）
                amount_cols = ['MainNetFlow', 'JumboNetFlow', 'SmallNetFlow', 'MidNetFlow', 'RetailNetFlow']
                for col in amount_cols:
                    if col in fund_df.columns:
                        fund_df[col] = fund_df[col].apply(_parse_chinese_amount)
                
                # DDX/DDY 列转换为数值
                ddx_cols = ['MainNetFlow3D', 'MainNetFlow5D', 'MainNetFlow10D', 'MainInflowDDX', 'MainInflowDDY']
                for col in ddx_cols:
                    if col in fund_df.columns:
                        fund_df[col] = pd.to_numeric(fund_df[col], errors='coerce')
            
            if margin_df is not None and not margin_df.empty:
                # 妙想融资融券列名映射
                mx_margin_map = {
                    '融资余额': 'FinanceValue',
                    '融资余额环比': 'FinanceValueDOD',
                    '融券余额': 'SecurityValue',
                    '融券余额环比': 'SecurityValueDOD',
                }
                for mx_col, ws_col in mx_margin_map.items():
                    if mx_col in margin_df.columns:
                        margin_df[ws_col] = margin_df[mx_col]
            
            result = {'fund': fund_df, 'margin': margin_df}
            _hub_cache[code] = result
            return result
    except Exception:
        pass  # fallback to stock_data_hub
    
    # ===== 回退到 stock_data_hub (westock-data) =====
    try:
        from stock_data_hub import StockDataHub
        hub = StockDataHub()
        
        # 统一 ticker 格式：使用适配器的 _to_stock_hub_symbol（支持 6 位数字、.SS/.SZ 格式等）
        if _HUB_ADAPTER_AVAILABLE:
            ticker_suffix = ".SS" if code.startswith('6') else ".SZ"
            ticker_full = _to_stock_hub_symbol(f"{code}{ticker_suffix}")
        else:
            ticker_full = f"sh{code}" if code.startswith('6') else f"sz{code}"
        
        fund_df = hub.get_fund_flow(ticker_full)
        margin_df = hub.get_margin_trade(ticker_full)
        
        # 数据新鲜度验证：检查返回数据的最新日期
        if _HUB_ADAPTER_AVAILABLE:
            expected_date = _get_last_trade_date()
            
            # 验证资金流向数据
            if fund_df is not None and not fund_df.empty and 'EndDate' in fund_df.columns:
                latest_date = str(fund_df['EndDate'].iloc[0])
                gap = _trade_day_diff(expected_date, latest_date)
                if gap > 3:
                    print(f"  [⚠️ 数据过期警告] {code} 资金流向: 最新日期 {latest_date}, 预期 {expected_date}, 差距 {gap} 交易日")
            
            # 验证融资融券数据
            if margin_df is not None and not margin_df.empty and 'date' in margin_df.columns:
                latest_date = str(margin_df['date'].iloc[0])
                gap = _trade_day_diff(expected_date, latest_date)
                if gap > 3:
                    print(f"  [⚠️ 数据过期警告] {code} 融资融券: 最新日期 {latest_date}, 预期 {expected_date}, 差距 {gap} 交易日")
        
        result = {'fund': fund_df, 'margin': margin_df}
        _hub_cache[code] = result
        return result
    except Exception:
        _hub_cache[code] = {'fund': None, 'margin': None}
        return _hub_cache[code]

# 尝试导入妙想选股
MX_XUANGU_AVAILABLE = False
try:
    from mx_xuangu import MXSelectStock
    MX_XUANGU_AVAILABLE = True
except ImportError:
    pass


# =====================================================================
# 扫描配置
# =====================================================================

# =====================================================================
# 统一初筛配置（Tier 1）
# =====================================================================
# 初筛目的：排除明显不好的标的，保留足够大的候选池供二级评分
# 不应该在初筛就过于严格，否则会漏掉好股票
# =====================================================================

SCAN_FILTERS = {
    # 价格筛选 — 排除极端涨跌停
    "price_change_max": 10.0,       # 日涨幅 ≤ 10%
    "price_change_min": -8.0,       # 日跌幅 ≥ -8%

    # 成交量筛选 — 排除流动性过差
    "turnover_min": 100_000_000,    # 日成交额 ≥ 1 亿

    # 资金面筛选 — 排除主力大幅出逃
    "main_inflow_min": 5_000_000,   # 主力净流入 ≥ 500 万
    "require_main_inflow": True,    # 必须满足主力流入（只要正向即可）

    # 技术面初筛 — 排除极度超买/超卖，保留可信号空间
    "rsi_min": 10,                  # RSI ≥ 10
    "rsi_max": 85,                  # RSI ≤ 85

    # 不在初筛强制技术信号（交给二级评分加分/过滤）
    "require_macd_golden": False,
    "require_kdj_golden": False,
    "require_ma_cross": False,

    # 不在初筛限制换手率/量比/估值（交给二级评分）
    "turnover_ratio_min": 0.0,      # 不限
    "volume_ratio_min": 0.0,        # 不限
    "pe_ttm_max": 0.0,              # 不限
    "pb_max": 0.0,                  # 不限

    # 不在初筛过情绪分（交给二级过滤）
    "sentiment_score_min": 0,

    # 排除
    "exclude_st": True,
    "exclude_gem": False,
    "exclude_star": False,
}

# =====================================================================
# 策略档位：二级评分阈值配置（Tier 2）
# =====================================================================
# 使用方式: scan_market(strategy="conservative", top_n=5)
# 初筛统一，仅在二级评分时按档位应用不同的过滤阈值
# =====================================================================

STRATEGY_PRESETS = {
    "conservative": {
        # --- 保守型：高确定性，只要精品 ---
        # 趋势信号已在评分中占 20%权重，有信号的股票自然得分高
        # 不强制要求，而是通过高评分线筛选
        "description": "高确定性：高评分线 + 估值合理 + 高情绪分",
        "score_min": 30,                    # 综合评分 ≥ 30
        "rsi_min": 30,                      # 二筛 RSI ≥ 30
        "rsi_max": 65,                      # 二筛 RSI ≤ 65
        "sentiment_score_min": 50,          # 情绪分 ≥ 50
        "require_any_trend_signal": False,  # 不强制趋势信号（评分自然筛选）
        "pe_ttm_max": 50.0,                 # PE < 50
        "pb_max": 4.0,                      # PB < 4
    },
    "balanced": {
        # --- 平衡型：适度筛选，基本面过关 ---
        "description": "平衡型：适度要求，追求收益与风险均衡",
        "score_min": 20,                    # 综合评分 ≥ 20
        "rsi_min": 15,                      # 二筛 RSI ≥ 15
        "rsi_max": 80,                      # 二筛 RSI ≤ 80
        "sentiment_score_min": 30,          # 情绪分 ≥ 30
        "require_any_trend_signal": False,  # 不强制趋势信号
        "pe_ttm_max": 100.0,                # PE < 100
        "pb_max": 10.0,                     # PB < 10
    },
    "aggressive": {
        # --- 激进型：宽松过滤，最大化弹性 ---
        "description": "激进型：宽松条件，追求高弹性和活跃度",
        "score_min": 15,                    # 综合评分 ≥ 15
        "rsi_min": 10,                      # 二筛 RSI ≥ 10
        "rsi_max": 80,                      # 二筛 RSI ≤ 80
        "sentiment_score_min": 30,          # 惇绪分 ≥ 30
        "require_any_trend_signal": False,  # 不强制趋势信号
        "pe_ttm_max": 0.0,                  # 不限 PE
        "pb_max": 0.0,                      # 不限 PB
    },
}


def get_strategy_config(strategy: str = "balanced") -> dict:
    """
    获取策略档位的二级评分配置.

    Args:
        strategy: "conservative" (保守) | "balanced" (平衡) | "aggressive" (激进)

    Returns:
        dict: 二级评分阈值配置
    """
    if strategy not in STRATEGY_PRESETS:
        valid = list(STRATEGY_PRESETS.keys())
        print(f"[WARN] 未知策略 '{strategy}'，使用平衡型。可选: {valid}")
        strategy = "balanced"
    config = dict(STRATEGY_PRESETS[strategy])
    print(f"[INFO] 已加载二级评分档位: {strategy} — {config['description']}")
    return config


TOP_N = 5                        # 深度分析前 N 只
MAX_SCAN_RETRIES = 3             # AkShare 连接重试次数
SCAN_TIMEOUT = 60                # 每次扫描超时（秒）

# 备选股票列表（当 AkShare 不可用时使用）
WATCHLIST = [
    # "中天科技",
    # "立讯精密",
    # "茅台",
    # "比亚迪",
    # "中国中免",
]


# =====================================================================
# Layer 1: 全市场扫描
# =====================================================================

def _fetch_all_spot_with_retry() -> Optional[Any]:
    """带重试机制的全市场实时行情获取 (AkShare)."""
    try:
        import akshare as ak
    except ImportError:
        print("[ERROR] akshare 未安装")
        return None

    for attempt in range(1, MAX_SCAN_RETRIES + 1):
        try:
            print(f"[信息] 正在通过 AkShare 获取全市场实时行情... (尝试 {attempt}/{MAX_SCAN_RETRIES})")
            df = ak.stock_zh_a_spot_em()
            print(f"[SUCCESS] AkShare 获取成功，共 {len(df)} 只股票")
            return df
        except Exception as e:
            print(f"[WARN] 第 {attempt} 次获取失败: {str(e)[:80]}")
            if attempt < MAX_SCAN_RETRIES:
                wait = attempt * 3
                print(f"[信息] {wait}秒后重试...")
                time.sleep(wait)
            else:
                print("[ERROR] AkShare 所有重试均失败")
    return None


# -----------------------------------------------------------------
# 妙想选股数据源
# -----------------------------------------------------------------

def _parse_mx_amount(val: str) -> float:
    """解析妙想返回的金额字符串，转为元.
    e.g. '22.26亿' -> 2226000000, '1500万' -> 15000000
    """
    if not val or val in ('--', '', 'NaN', 'None'):
        return 0.0
    s = str(val).strip().replace(',', '')
    # 提取数字
    m = re.search(r'([+-]?\d+\.?\d*)', s)
    if not m:
        return 0.0
    num = float(m.group(1))
    if '亿' in s:
        return num * 1e8
    elif '万' in s:
        return num * 1e4
    else:
        return num


def _fetch_candidates_via_mx(filters: dict) -> Optional[List[Dict[str, Any]]]:
    """
    通过妙想智能选股获取候选股票列表.
    返回格式与 AkShare 筛选后的 candidates 相同.
    """
    if not MX_XUANGU_AVAILABLE:
        print("[INFO] 妙想选股模块未可用，跳过")
        return None

    print("[信息] 正在通过妙想智能选股获取候选股票...")

    try:
        mx = MXSelectStock()

        # 构建选股查询语句（基于筛选条件）
        query_parts = ["今日A股"]

        turnover_min = filters.get('turnover_min', 0)
        if turnover_min >= 1e8:
            query_parts.append(f"成交额超过{turnover_min/1e8:.0f}亿")
        elif turnover_min >= 1e4:
            query_parts.append(f"成交额超过{turnover_min/1e4:.0f}万")

        main_inflow_min = filters.get('main_inflow_min', 0)
        if main_inflow_min >= 1e8:
            query_parts.append(f"主力净流入超过{main_inflow_min/1e8:.0f}亿")
        elif main_inflow_min >= 1e4:
            query_parts.append(f"主力净流入超过{main_inflow_min/1e4:.0f}万")

        price_max = filters.get('price_change_max', 999)
        price_min = filters.get('price_change_min', -999)
        if price_max < 999:
            query_parts.append(f"涨幅不超过{price_max}%")
        if price_min > -999:
            query_parts.append(f"涨幅不低于{price_min}%")

        # 排除 ST
        if filters.get('exclude_st', True):
            query_parts.append("排除ST股票")

        # RSI 条件
        rsi_min = filters.get('rsi_min', 0)
        rsi_max = filters.get('rsi_max', 100)
        if rsi_min > 0 and rsi_max < 100:
            query_parts.append(f"RSI在{rsi_min}到{rsi_max}之间")
        elif rsi_min > 0:
            query_parts.append(f"RSI大于{rsi_min}")
        elif rsi_max < 100:
            query_parts.append(f"RSI小于{rsi_max}")

        # 可选技术信号
        if filters.get('require_macd_golden', False):
            query_parts.append("MACD金叉")
        if filters.get('require_kdj_golden', False):
            query_parts.append("KDJ金叉")
        if filters.get('require_ma_cross', False):
            query_parts.append("MA5上穿MA10")

        # 换手率
        turnover_ratio_min = filters.get('turnover_ratio_min', 0)
        if turnover_ratio_min > 0:
            query_parts.append(f"换手率超过{turnover_ratio_min}%")

        # 量比
        volume_ratio_min = filters.get('volume_ratio_min', 0)
        if volume_ratio_min > 0:
            query_parts.append(f"量比大于{volume_ratio_min}")

        # 市盈率
        pe_ttm_max = filters.get('pe_ttm_max', 0)
        if pe_ttm_max > 0:
            query_parts.append(f"市盈率低于{pe_ttm_max}")

        # 市净率
        pb_max = filters.get('pb_max', 0)
        if pb_max > 0:
            query_parts.append(f"市净率低于{pb_max}")

        query = "、".join(query_parts)
        print(f"[INFO] 妙想选股查询: {query}")

        result = mx.search(query)
        rows, data_source, err = mx.extract_data(result)

        # 妙想返回空数据（条件过于严格）→ 视为成功但空结果，不降级
        if err and "无有效" in str(err) and not rows:
            print("[INFO] 妙想选股：无匹配股票（条件过于严格），不降级")
            return []

        if err:
            print(f"[WARN] 妙想选股返回错误: {err}")
            return None

        if not rows:
            print("[INFO] 妙想选股返回结果为空（条件过于严格），不降级")
            return []

        print(f"[SUCCESS] 妙想选股获取成功，共 {len(rows)} 只股票 (来源: {data_source})")

        # 解析行数据
        candidates = []
        for row in rows:
            # 局部变量：基础字段
            code = None
            name = None
            price = 0.0
            change_pct = 0.0
            turnover = 0.0
            main_inflow = 0.0
            rsi = None
            # 局部变量：扩展技术字段
            macd_golden = False
            kdj_golden = False
            ma5_cross_ma10 = False
            ma5 = None
            ma10 = None
            turnover_ratio = None
            volume_ratio = None
            boll_mid = None
            pe_ttm = None
            pe_dyn = None
            pb = None

            for k, v in row.items():
                k_lower = k.lower()
                if '代码' in k and '市场' not in k:
                    code = str(v).strip()
                elif '名称' in k or '名字' in k:
                    name = str(v).strip()
                elif '最新价' in k or '价格' in k:
                    price = _parse_numeric(v)
                elif '涨跌幅' in k or '涨跌' in k:
                    change_pct = _parse_numeric(v)
                elif '成交额' in k and '市' not in k.lower():
                    turnover = _parse_mx_amount(v)
                elif '主力净额' in k or '主力净流入' in k or '主力' in k:
                    main_inflow = _parse_mx_amount(v)
                elif 'rsi' in k_lower or 'RSI' in k:
                    rsi_val = _parse_numeric(v)
                    if rsi_val is not None and rsi_val > 0:
                        rsi = rsi_val
                elif 'macd金叉' in k_lower:
                    macd_golden = str(v).strip() == '符合'
                elif 'kdj金叉' in k_lower:
                    kdj_golden = str(v).strip() == '符合'
                elif '5日均线上穿10日均线' in k:
                    ma5_cross_ma10 = str(v).strip() == '符合'
                elif '5日均线(元)' in k or ('5日均线' in k and '上穿' not in k):
                    ma5 = _parse_numeric(v)
                elif '10日均线(元)' in k:
                    ma10 = _parse_numeric(v)
                elif '换手率' in k:
                    turnover_ratio = _parse_numeric(v)
                elif '量比' in k and '换' not in k:
                    volume_ratio = _parse_numeric(v)
                elif 'boll值' in k_lower:
                    boll_mid = _parse_numeric(v)
                elif '市盈率' in k and 'ttm' in k_lower:
                    pe_ttm = _parse_numeric(v)
                elif '市盈率(动)' in k:
                    pe_dyn = _parse_numeric(v)
                elif '市净率' in k:
                    pb = _parse_numeric(v)

            if not code or not name:
                continue

            # 排除 ST
            if filters.get('exclude_st', True) and ('ST' in name or '*ST' in name):
                continue

            # 排除创业板
            if filters.get('exclude_gem', False) and code.startswith(('300', '301')):
                continue

            # 排除科创板
            if filters.get('exclude_star', False) and code.startswith('688'):
                continue

            # 基础筛选
            if change_pct > filters.get('price_change_max', 999):
                continue
            if change_pct < filters.get('price_change_min', -999):
                continue
            if turnover < filters.get('turnover_min', 0):
                continue

            # 主力流入筛选
            if filters.get('require_main_inflow', False):
                if main_inflow < filters.get('main_inflow_min', 0):
                    continue

            candidates.append({
                'code': code,
                'name': name,
                'price': price,
                'change_pct': change_pct,
                'turnover': turnover,
                'turnover_million': turnover / 1_000_000,
                'main_inflow': main_inflow,
                'main_inflow_million': main_inflow / 1_000_000,
                'rsi': rsi,
                'sentiment_score': None,
                'score': 0,
                # 扩展技术字段
                'macd_golden': macd_golden,
                'kdj_golden': kdj_golden,
                'ma5_cross_ma10': ma5_cross_ma10,
                'ma5': ma5,
                'ma10': ma10,
                'turnover_ratio': turnover_ratio,
                'volume_ratio': volume_ratio,
                'boll_mid': boll_mid,
                'pe_ttm': pe_ttm,
                'pe_dyn': pe_dyn,
                'pb': pb,
            })

        print(f"[信息] 妙想选股筛选后剩余 {len(candidates)} 只")
        return candidates

    except Exception as e:
        print(f"[WARN] 妙想选股获取失败: {str(e)[:100]}")
        return None


def _parse_numeric(val) -> float:
    """将字符串/数值转为浮点数，处理 '--' 等异常值."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(',', '').replace('%', '')
    if s in ('--', '', 'NaN', 'None', 'null'):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _get_rsi_for_code(code: str, max_retries: int = 2) -> Optional[float]:
    """获取单只股票的 RSI（快速查询）.
    
    优先使用妙想(MX)查询，失败时回退到 stock_data_hub.
    """
    # ===== 优先尝试妙想(MX) =====
    try:
        import sys
        from pathlib import Path
        home = str(Path.home())
        for p in [f"{home}/mx-data", f"{home}/mx-search"]:
            if p not in sys.path:
                sys.path.insert(0, p)
        import mx_data
        
        client = mx_data.MXData()
        query = f"{code} RSI指标"
        result = client.query(query)
        if result.get("status") == 0:
            tables, _, _, err = mx_data.MXData.parse_result(result)
            if tables and not err:
                import pandas as pd
                df = pd.DataFrame(tables[0].get("rows", []))
                if not df.empty:
                    # 查找 RSI 列
                    rsi_col = None
                    for col in df.columns:
                        if 'rsi' in col.lower():
                            rsi_col = col
                            break
                    if rsi_col:
                        # 取最新一行
                        val = df[rsi_col].iloc[-1]
                        return float(val)
    except Exception:
        pass  # fallback to stock_data_hub
    
    # ===== 回退到 stock_data_hub (westock-data) =====
    try:
        from stock_data_hub import StockDataHub
        hub = StockDataHub()
        
        # 统一 ticker 格式
        if _HUB_ADAPTER_AVAILABLE:
            ticker_suffix = ".SS" if code.startswith('6') else ".SZ"
            sym = _to_stock_hub_symbol(f"{code}{ticker_suffix}")
        else:
            sym = f"sh{code}" if code.startswith('6') else f"sz{code}"
        
        df = hub.get_technical(sym, group='rsi', limit=5)
        if df is not None and len(df) > 0 and 'rsi' in df.columns:
            rsi_val = float(df['rsi'].iloc[-1])
            
            # 数据新鲜度验证：RSI 数据通常包含日期列
            if _HUB_ADAPTER_AVAILABLE and 'date' in df.columns:
                expected_date = _get_last_trade_date()
                latest_date = str(df['date'].iloc[-1])
                gap = _trade_day_diff(expected_date, latest_date)
                if gap > 3:
                    print(f"  [⚠️ 数据过期警告] {code} RSI: 最新日期 {latest_date}, 预期 {expected_date}, 差距 {gap} 交易日")
            
            return rsi_val
    except Exception:
        pass
    return None


def _get_sentiment_for_code(code: str) -> Optional[float]:
    """获取情绪分 (妙想资讯搜索优先, 后fallback AkShare千股千评)."""
    try:
        from tradingagents.dataflows.sentiment_mx import get_sentiment_score
        return get_sentiment_score(code)
    except Exception:
        pass
    return None


def scan_market(filters: dict = None, strategy: str = "balanced", top_n: int = 10, skip_llm: bool = False) -> List[Dict[str, Any]]:
    """
    扫描全市场，多条件筛选，返回候选股票列表.

    架构：两级筛选
        Tier 1: 统一初筛 — 排除明显不好的标的（主力流入、成交额、极端涨跌停、ST）
        Tier 2: 按档位二级评分 — 多维度加权评分 + 按档位过滤

    数据源优先级:
        1. 妙想智能选股 (mx_xuangu) — 主要路径，高效且含技术指标
        2. AkShare 全市场数据 (fallback) — 仅在妙想请求失败时触发
        3. WATCHLIST (最后备选) — 两者均失败时

    重要行为:
        - 妙想成功但返回 0 只 → 不降级，直接返回空列表（条件过于严格）
        - 妙想请求失败 → 自动降级到 AkShare

    使用方式:
        result = scan_market(strategy="conservative", top_n=5)  # 保守型
        result = scan_market(strategy="balanced", top_n=5)      # 平衡型 (默认)
        result = scan_market(strategy="aggressive", top_n=5)    # 激进型

    Args:
        filters: 初筛配置（默认用 SCAN_FILTERS）
        strategy: 二级评分档位 "conservative" | "balanced" | "aggressive"
        top_n: 返回前 N 只

    Returns:
        List[dict]: 每个 dict 包含 code, name, price, change_pct, turnover,
                    rsi, sentiment_score, macd_golden, kdj_golden, score, score_details 等
    """
    filters = filters or SCAN_FILTERS
    strategy_config = get_strategy_config(strategy)

    # ===== 优先尝试妙想选股 =====
    candidates = _fetch_candidates_via_mx(filters)

    # 妙想成功但返回空列表（条件过于严格），不降级、不走 WATCHLIST，直接返回
    if candidates == []:
        print("[INFO] 妙想选股：条件过于严格，无匹配股票，不降级")
        return []

    # ===== Fallback 到 AkShare =====
    if candidates is None:
        df = _fetch_all_spot_with_retry()

        if df is not None:
            print(f"\n[信息] 开始筛选，原始数据 {len(df)} 只...")

            candidates = []
            for _, row in df.iterrows():
                code = str(row.get('代码', '')).strip()
                name = str(row.get('名称', '')).strip()
                if not code or not name:
                    continue

                # 排除 ST
                if filters.get('exclude_st', True) and ('ST' in name or '*ST' in name):
                    continue

                # 排除创业板 (300/301 开头)
                if filters.get('exclude_gem', False) and code.startswith(('300', '301')):
                    continue

                # 排除科创板 (688 开头)
                if filters.get('exclude_star', False) and code.startswith('688'):
                    continue

                change_pct = _parse_numeric(row.get('涨跌幅'))
                turnover = _parse_numeric(row.get('成交额'))  # 单位：元
                main_inflow = _parse_numeric(row.get('主力净流入'))  # 单位：元

                # 价格筛选
                if change_pct > filters.get('price_change_max', 999):
                    continue
                if change_pct < filters.get('price_change_min', -999):
                    continue
                if turnover < filters.get('turnover_min', 0):
                    continue

                # 资金面筛选 — 核心：主力流入
                if filters.get('require_main_inflow', False):
                    if main_inflow < filters.get('main_inflow_min', 0):
                        continue

                # 尝试从 AkShare spot 数据获取更多字段
                turnover_ratio = _parse_numeric(row.get('换手率'))
                volume_ratio = _parse_numeric(row.get('量比'))
                # PE: 优先用动态市盈率，没有则用 TTM
                pe_dyn = _parse_numeric(row.get('市盈率-动态'))
                pe_ttm = _parse_numeric(row.get('市盈率'))
                pe_val = pe_dyn if pe_dyn > 0 else pe_ttm
                pb = _parse_numeric(row.get('市净率'))
                
                candidates.append({
                    'code': code,
                    'name': name,
                    'price': _parse_numeric(row.get('最新价')),
                    'change_pct': change_pct,
                    'turnover': turnover,
                    'turnover_million': turnover / 1_000_000,  # 转为百万
                    'main_inflow': main_inflow,
                    'main_inflow_million': main_inflow / 1_000_000,
                    'rsi': None,           # 待填充
                    'sentiment_score': None,  # 待填充
                    'score': 0,            # 综合评分
                    # 扩展技术字段（MACD/KDJ/MA/BOLL AkShare spot 无法获取）
                    'macd_golden': None,
                    'kdj_golden': None,
                    'ma5_cross_ma10': None,
                    'ma5': None,
                    'ma10': None,
                    # 活跃度/估值字段（AkShare spot 可获取）
                    'turnover_ratio': turnover_ratio if turnover_ratio > 0 else None,
                    'volume_ratio': volume_ratio if volume_ratio > 0 else None,
                    'boll_mid': None,
                    'pe_ttm': pe_ttm if pe_ttm > 0 else None,
                    'pe_dyn': pe_dyn if pe_dyn > 0 else None,
                    'pb': pb if pb > 0 else None,
                })

            print(f"[信息] 基础筛选后剩余 {len(candidates)} 只")
        else:
            print("[WARN] AkShare 无法获取全市场数据")
            candidates = []

    if not candidates:
        return []

    # 进阶筛选：Tier 2 按档位评分 + 过滤
    # 为了效率，只对前 candidate_limit 只做进阶筛选
    CANDIDATE_LIMIT = max(top_n * 3, 20)  # 最多检查 20 只
    candidates_to_process = candidates[:CANDIDATE_LIMIT]
    print(f"[信息] 对前 {len(candidates_to_process)} 只做二级评分 (档位: {strategy})...")

    # 从策略配置读取二级过滤阈值
    tier2_rsi_min = strategy_config.get('rsi_min', 0)
    tier2_rsi_max = strategy_config.get('rsi_max', 100)
    tier2_sentiment_min = strategy_config.get('sentiment_score_min', 0)
    tier2_score_min = strategy_config.get('score_min', 0)
    tier2_require_trend = strategy_config.get('require_any_trend_signal', False)
    tier2_pe_max = strategy_config.get('pe_ttm_max', 0)
    tier2_pb_max = strategy_config.get('pb_max', 0)

    # 并行预加载 stock_data_hub 数据（避免循环内串行查询超时）
    print("[信息] 正在并行加载资金流向和融资融券数据...")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def _fetch_hub_single(code):
        try:
            return code, _get_hub_data(code)
        except Exception:
            return code, {'fund': None, 'margin': None}
    
    hub_data_map = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_hub_single, c['code']): c['code'] for c in candidates_to_process}
        for future in as_completed(futures):
            code, data = future.result()
            hub_data_map[code] = data
    print(f"[信息] Hub数据加载完成，共 {len(hub_data_map)} 只")
    
    filtered = []
    for c in candidates_to_process:
        # RSI: 如果妙想已返回RSI，直接使用；否则本地查询
        if c.get('rsi') is not None:
            rsi = c['rsi']
        else:
            rsi = _get_rsi_for_code(c['code'])
            c['rsi'] = rsi
        if rsi is not None and (rsi < tier2_rsi_min or rsi > tier2_rsi_max):
            continue

        # 情绪得分
        sentiment = _get_sentiment_for_code(c['code'])
        c['sentiment_score'] = sentiment
        if sentiment is not None and sentiment < tier2_sentiment_min:
            continue

        # =====================================================================
        # Tier 2: 多维度二级评分模型（每个维度 0–100 分，加权后合成）
        # =====================================================================
        score = 0
        score_details = []

        # -------- 维度 A: 资金面（权重 30% → 满分30分）--------
        cap = c
        inflow_score = min(cap['main_inflow_million'] / 20, 15)  # 每2000万=15分，上限15（权重下调）
        if cap['change_pct'] < 0 and cap['main_inflow_million'] > 50:
            inflow_score += 15
            score_details.append(f"资金面:逆势大单吸筹+{inflow_score:.1f}")
        elif cap['change_pct'] < 0 and cap['main_inflow_million'] > 20:
            inflow_score += 10
            score_details.append(f"资金面:跌势主力探底+{inflow_score:.1f}")
        else:
            score_details.append(f"资金面:主力流入+{inflow_score:.1f}")
        score += inflow_score

        # -------- 维度 B: 趋势信号（权重 20% → 满分20分）--------
        trend_score = 0
        if cap.get('macd_golden'):
            trend_score += 10
            score_details.append("趋势:MACD金叉+10")
        if cap.get('kdj_golden'):
            trend_score += 5
            score_details.append("趋势:KDJ金叉+5")
        if cap.get('ma5_cross_ma10'):
            trend_score += 5
            score_details.append("趋势:MA5上穿MA10+5")
        if trend_score == 0:
            # 若无明确信号，按涨跌幅给基础分
            if cap['change_pct'] > 0:
                trend_score = min(cap['change_pct'] * 1.0, 10)
                score_details.append(f"趋势:上涨势头+{trend_score:.1f}")
            elif cap['change_pct'] > -2:
                trend_score = 2
                score_details.append("趋势:小跌持平+2")
        score += trend_score

        # -------- 维度 C: 活跃度（权重 20% → 满分20分）--------
        active_score = 0
        tr = cap.get('turnover_ratio')
        vr = cap.get('volume_ratio')
        if tr is not None and tr >= 5:
            active_score += 8
            score_details.append(f"活跃度:换手率{tr:.1f}%+8")
        elif tr is not None and tr >= 2:
            active_score += 4
            score_details.append(f"活跃度:换手率{tr:.1f}%+4")
        if vr is not None and vr >= 2:
            active_score += 5
            score_details.append(f"活跃度:量比{vr:.1f}+5")
        elif vr is not None and vr >= 1:
            active_score += 2
            score_details.append(f"活跃度:量比{vr:.1f}+2")
        if cap['turnover_million'] > 500:
            active_score += 7
            score_details.append("活跃度:大成交额+7")
        elif cap['turnover_million'] > 200:
            active_score += 3
            score_details.append("活跃度:成交额+3")
        score += active_score

        # -------- 维度 D: 估值（权重 10% → 满分10分）--------
        val_score = 0
        pe = cap.get('pe_ttm')
        pb = cap.get('pb')
        if pe is not None and 0 < pe < 20:
            val_score += 6
            score_details.append(f"估值:PE{pe:.1f}低+6")
        elif pe is not None and 0 < pe < 40:
            val_score += 3
            score_details.append(f"估值:PE{pe:.1f}适中+3")
        if pb is not None and 0 < pb < 2:
            val_score += 4
            score_details.append(f"估值:PB{pb:.1f}低+4")
        elif pb is not None and 0 < pb < 3:
            val_score += 2
            score_details.append(f"估值:PB{pb:.1f}适中+2")
        score += val_score

        # -------- 维度 E: RSI 健康（权重 7% → 满分7分）--------
        rsi_score = 0
        rsi_val = cap.get('rsi')
        if rsi_val and 40 <= rsi_val <= 60:
            rsi_score = 7
            score_details.append(f"RSI:健康区{rsi_val:.0f}+7")
        elif rsi_val and 30 <= rsi_val <= 70:
            rsi_score = 3
            score_details.append(f"RSI:可接受区{rsi_val:.0f}+3")
        score += rsi_score

        # -------- 维度 F: 情绪分（权重 8% → 满分8分）--------
        sent_score = 0
        if cap['sentiment_score'] and cap['sentiment_score'] > 70:
            sent_score = 8
            score_details.append(f"情绪:{cap['sentiment_score']:.0f}分+8")
        elif cap['sentiment_score'] and cap['sentiment_score'] > 55:
            sent_score = 5
            score_details.append(f"情绪:{cap['sentiment_score']:.0f}分+5")
        elif cap['sentiment_score'] and cap['sentiment_score'] > 40:
            sent_score = 2
            score_details.append(f"情绪:{cap['sentiment_score']:.0f}分+2")
        score += sent_score

        # -------- 维度 G: 资金趋势（权重 10% → 满分10分）--------
        trend_inflow_score = 0
        # 使用预加载的 hub 数据
        try:
            hub_data = hub_data_map.get(cap['code'], {'fund': None, 'margin': None})
            df = hub_data.get('fund')
            if df is not None and not df.empty:
                row = df.iloc[0]
                main_5d = float(row.get('MainNetFlow5D', 0) or 0)
                main_10d = float(row.get('MainNetFlow10D', 0) or 0)
                main_20d = float(row.get('MainNetFlow20D', 0) or 0)
                jumbo = float(row.get('JumboNetFlow', 0) or 0)
                small = float(row.get('SmallNetFlow', 0) or 0)
                main_inflow_today = float(row.get('MainNetFlow', 0) or 0)
                
                # 5日/10日/20日趋势评分
                if main_5d > 0:
                    trend_inflow_score += 3
                    score_details.append("资金趋势:5日主力转正+3")
                if main_10d > 0:
                    trend_inflow_score += 2
                    score_details.append("资金趋势:10日主力转正+2")
                if main_20d > 0:
                    trend_inflow_score += 2
                    score_details.append("资金趋势:20日主力转正+2")
                
                # 散户-主力背离（经典吸筹信号）
                # 条件放宽：散户卖 + 当日主力买（因为初筛已保证当日主力流入）
                if small < 0 and main_inflow_today > 0:
                    trend_inflow_score += 3
                    score_details.append("资金趋势:散户卖主力买+3")
                elif small < 0:
                    trend_inflow_score += 1
                    score_details.append("资金趋势:散户流出+1")
                
                # 超大单机构信号
                if jumbo > main_inflow_today * 0.5:
                    trend_inflow_score += 2
                    score_details.append("资金趋势:超大单主导+2")
            else:
                score_details.append("资金趋势:无数据+0")
        except Exception:
            score_details.append("资金趋势:查询失败+0")
        score += min(trend_inflow_score, 10)

        # -------- 维度 H: 杠杆情绪（权重 5% → 满分5分）--------
        leverage_score = 0
        try:
            hub_data = hub_data_map.get(cap['code'], {'fund': None, 'margin': None})
            df = hub_data.get('margin')
            if df is not None and not df.empty:
                row = df.iloc[0]
                finance_dod = float(row.get('FinanceValueDOD', 0) or 0)
                security_dod = float(row.get('SecurityValueDOD', 0) or 0)
                
                # 融资余额变化
                if finance_dod > 3:
                    leverage_score += 3
                    score_details.append(f"杠杆:融资大增{finance_dod:.1f}%+3")
                elif finance_dod > 1:
                    leverage_score += 2
                    score_details.append(f"杠杆:融资增{finance_dod:.1f}%+2")
                elif finance_dod > 0:
                    leverage_score += 1
                    score_details.append(f"杠杆:融资微增{finance_dod:.1f}%+1")
                elif finance_dod < -3:
                    leverage_score -= 2
                    score_details.append(f"杠杆:融资大减{finance_dod:.1f}%-2")
                
                # 融券余额变化（空头信号）
                if security_dod > 10:
                    leverage_score -= 2
                    score_details.append(f"杠杆:融券大增{security_dod:.1f}%-2")
                elif security_dod < -10:
                    leverage_score += 1
                    score_details.append(f"杠杆:融券大减{security_dod:.1f}%+1")
            else:
                score_details.append("杠杆:无数据+0")
        except Exception as e:
            score_details.append(f"杠杆:查询失败({str(e)[:30]})+0")
        score += max(min(leverage_score, 5), -3)

        # -------- 维度 I: 机构行为（权重 5% → 满分5分）--------
        inst_score = 0
        try:
            hub_data = _get_hub_data(cap['code'])
            df = hub_data.get('fund')
            if df is not None and not df.empty:
                row = df.iloc[0]
                block_net = float(row.get('BlockNetFlow', 0) or 0)
                block_info = row.get('BlockTradingInfos', '')
                
                if block_info:
                    import json
                    try:
                        block_data = json.loads(block_info)
                        if block_data:
                            # 计算平均折价率
                            discounts = []
                            for item in block_data:
                                price = float(item.get('TurnoverPrice', 0) or 0)
                                if price > 0 and cap['price'] > 0:
                                    discount = (price - cap['price']) / cap['price'] * 100
                                    discounts.append(discount)
                            
                            if discounts:
                                avg_discount = sum(discounts) / len(discounts)
                                if avg_discount < -20:
                                    inst_score -= 5
                                    score_details.append(f"机构:大宗折价{avg_discount:.1f}%-5")
                                elif avg_discount < -10:
                                    inst_score -= 3
                                    score_details.append(f"机构:大宗折价{avg_discount:.1f}%-3")
                                elif avg_discount < -5:
                                    inst_score -= 1
                                    score_details.append(f"机构:大宗折价{avg_discount:.1f}%-1")
                                elif avg_discount > 0:
                                    inst_score += 2
                                    score_details.append(f"机构:大宗溢价{avg_discount:.1f}%+2")
                                else:
                                    score_details.append("机构:大宗平价+0")
                            else:
                                score_details.append("机构:无大宗数据+0")
                        else:
                            score_details.append("机构:无大宗交易+0")
                    except Exception:
                        score_details.append("机构:大宗解析失败+0")
                else:
                    score_details.append("机构:无大宗交易+0")
            else:
                score_details.append("机构:无数据+0")
        except Exception as e:
            score_details.append(f"机构:查询失败({str(e)[:30]})+0")
        score += max(min(inst_score, 5), -5)


        c['score'] = round(score, 2)
        c['score_details'] = score_details

        # -------- 按策略档位过滤 --------
        # 1. 评分线
        if score < tier2_score_min:
            continue

        # 2. 趋势信号强制
        if tier2_require_trend:
            has_signal = cap.get('macd_golden') or cap.get('kdj_golden') or cap.get('ma5_cross_ma10')
            if not has_signal:
                continue

        # 3. 估值约束
        if tier2_pe_max > 0:
            pe_val = cap.get('pe_ttm')
            if pe_val is not None and pe_val > 0 and pe_val > tier2_pe_max:
                continue
        if tier2_pb_max > 0:
            pb_val = cap.get('pb')
            if pb_val is not None and pb_val > 0 and pb_val > tier2_pb_max:
                continue

        filtered.append(c)

    print(f"[信息] 二级评分后剩余 {len(filtered)} 只")

    # 按综合评分排序，取 Top N
    filtered.sort(key=lambda x: x['score'], reverse=True)
    return filtered[:top_n]


# =====================================================================
# Layer 2: 浅度分析
# =====================================================================

def analyze_candidates(candidates: List[Dict[str, Any]], mode: str = "medium") -> List[Dict[str, Any]]:
    """
    对候选列表进行并行后台分析.

    使用 ThreadPoolExecutor 并发启动多个独立 Python 进程跑 analyze_ashare.py，
    每只股票的 LLM 分析在独立后台进程中执行，互不阻塞.
    """
    total = len(candidates)
    print(f"\n{'='*70}")
    print(f"📊 阶段 2/2: 并行后台分析 {total} 只候选股票")
    print(f"   模式: {mode} | 并发数: 3 | 每只约 2-5 分钟")
    print(f"{'='*70}")

    ANALYSIS_SCRIPT = Path("/home/ubuntu/tradingagents-fork/analyze_ashare.py")

    def _run_single(idx: int, c: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        name = c['name']
        code = c['code']
        ticker = f"{code}.SS" if code.startswith(('6', '9')) else f"{code}.SZ"

        cmd = [
            sys.executable, str(ANALYSIS_SCRIPT),
            "--stock", ticker,
            "--mode", mode,
            "--analysts", "all",
        ]

        # 重试配置
        max_retries = 2
        base_delay = 30  # 秒

        for attempt in range(max_retries + 1):
            attempt_label = f"(重试{attempt}/{max_retries})" if attempt > 0 else ""
            print(f"  [{idx}/{total}] 🚀 启动后台分析: {name} ({ticker}) {attempt_label}")

            try:
                result = subprocess.run(
                    cmd,
                    cwd="/home/ubuntu/tradingagents-fork",
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10 分钟单只超时
                    env={**os.environ, "STOCK_DATA_HUB_MX_FIRST": "1"},
                )

                if result.returncode == 0:
                    # 成功，跳出重试循环
                    break

                # 失败，检查是否需要重试
                err = result.stderr[-300:] if result.stderr else "未知错误"
                is_retryable = any(kw in err.lower() for kw in [
                    'service_unavailable', '503', 'rate limit', '429',
                    'timeout', 'connection', 'temporarily unavailable'
                ])

                if is_retryable and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)  # 指数退避: 30s, 60s
                    print(f"  [{idx}/{total}] ⚠️ {name} 可重试错误，{delay}秒后重试: {err[:150]}")
                    time.sleep(delay)
                    continue  # 重试
                else:
                    # 不可重试或已用尽重试次数
                    print(f"  [{idx}/{total}] ❌ {name} 分析失败: {err[:200]}")
                    c['analysis_result'] = {
                        'decision': f"后台分析失败: {err[:150]}",
                        'rating': "ERROR",
                        'report_file': None,
                    }
                    return idx, c

            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    print(f"  [{idx}/{total}] ⏰ {name} 超时，{delay}秒后重试")
                    time.sleep(delay)
                    continue
                else:
                    print(f"  [{idx}/{total}] ⏰ {name} 分析超时（超过10分钟，已重试{max_retries}次）")
                    c['analysis_result'] = {
                        'decision': "后台分析超时(超过10分钟)",
                        'rating': "ERROR",
                        'report_file': None,
                    }
                    return idx, c
            except Exception as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    print(f"  [{idx}/{total}] ⚠️ {name} 异常，{delay}秒后重试: {e}")
                    time.sleep(delay)
                    continue
                else:
                    print(f"  [{idx}/{total}] ❌ {name} 分析异常: {e}")
                    c['analysis_result'] = {
                        'decision': f"后台分析异常: {str(e)[:100]}",
                        'rating': "ERROR",
                        'report_file': None,
                    }
                    return idx, c

        # 读取最新生成的报告
        results_dir = Path("stock_analysis_result")
        report_files = sorted(results_dir.glob(f"{ticker}_*_report.json"), reverse=True)
        if report_files:
            with open(report_files[0], 'r', encoding='utf-8') as f:
                report = json.load(f)
            decision = report.get('final_trade_decision', '')
            rating = _extract_rating(decision)
            trader_plan = report.get('trader_investment_plan', '')
            investment_plan = report.get('investment_plan', '')
            retry_tag = " [重试成功]" if attempt > 0 else ""
            print(f"  [{idx}/{total}] ✅ {name} → 评级: {rating}{retry_tag}")
        else:
            decision = "未生成报告"
            rating = "UNKNOWN"
            trader_plan = ""
            investment_plan = ""
            print(f"  [{idx}/{total}] ⚠️ {name} → 未找到报告")

        c['analysis_result'] = {
            'decision': decision[:500] if decision else '',
            'rating': rating,
            'report_file': str(report_files[0]) if report_files else None,
            # 结构化字段
            'action': _extract_action(trader_plan),
            'stop_loss': _extract_stop_loss(decision, trader_plan),
            'price_target': _extract_price_target(decision),
            'rationale': _extract_rationale(investment_plan, decision),
            'key_risks': _extract_key_risks(decision),
            'watch_points': _extract_watch_points(decision),
            'bull_bear_summary': _extract_bull_bear_summary(investment_plan, decision),
        }
        return idx, c

    # 并行执行（max_workers=3 避免 LLM API 过载）
    results_with_idx = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_run_single, idx, c): idx for idx, c in enumerate(candidates, 1)}
        for future in as_completed(futures):
            results_with_idx.append(future.result())

    # 按原始顺序排序
    results_with_idx.sort(key=lambda x: x[0])
    results = [r[1] for r in results_with_idx]

    print(f"\n{'='*70}")
    print(f"✅ 后台分析全部完成！{total} 只中:")
    rating_counts = {}
    for r in results:
        rt = r.get('analysis_result', {}).get('rating', 'UNKNOWN')
        rating_counts[rt] = rating_counts.get(rt, 0) + 1
    for rt, cnt in sorted(rating_counts.items()):
        print(f"   {rt}: {cnt} 只")
    print(f"{'='*70}")

    return results


def _extract_rating(decision_text: str) -> str:
    """从 Portfolio Manager 决策文本中提取评级."""
    if not decision_text:
        return "UNKNOWN"
    text = decision_text.upper()
    for keyword in ['SELL', 'UNDERWEIGHT', 'HOLD', 'OVERWEIGHT', 'BUY']:
        if keyword in text:
            return keyword
    return "UNKNOWN"


def _extract_action(trader_plan: str) -> str:
    """从 Trader Plan 文本中提取 Action (Buy/Hold/Sell)."""
    if not trader_plan:
        return "Unknown"
    # 尝试匹配 **Action**: XXX 格式
    import re
    m = re.search(r'\*\*Action\*\*[:\s]*([A-Za-z]+)', trader_plan)
    if m:
        return m.group(1).upper()
    # 备选: FINAL TRANSACTION PROPOSAL
    m = re.search(r'FINAL TRANSACTION PROPOSAL[:\s]*\*\*([A-Za-z]+)\*\*', trader_plan)
    if m:
        return m.group(1).upper()
    # 更宽松的匹配
    for kw in ['SELL', 'HOLD', 'BUY']:
        if kw in trader_plan.upper():
            return kw
    return "Unknown"


def _extract_stop_loss(decision_text: str, trader_plan: str) -> str:
    """从决策文本中提取止损价."""
    import re
    text = decision_text + "\n" + trader_plan
    # 尝试匹配 止损 或 Stop Loss（支持 markdown 格式 **止损**）
    m = re.search(r'(?:\*\*)?(?:止损(?:基准?|线?|价)?|Stop\s*Loss)(?:\*\*)?[:：\s]*([\d\.]+)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/A"


def _extract_price_target(decision_text: str) -> str:
    """从决策文本中提取目标价."""
    import re
    m = re.search(r'(?:目标价|Price\s*Target)[:：\s]*([\d\-\.\uff5e~\s]+)', decision_text, re.IGNORECASE)
    if m:
        return m.group(1).strip().replace(' ', '')
    return "N/A"


def _extract_rationale(investment_plan: str, decision_text: str) -> str:
    """从 investment_plan 或 decision_text 中提取核心依据."""
    if investment_plan:
        # 提取 **Rationale** 后的内容
        import re
        m = re.search(r'\*\*Rationale\*\*[:\s]*(.{50,300}?)(?:\n\n|\n##|\n\*\*)', investment_plan, re.DOTALL)
        if m:
            return m.group(1).strip().replace('\n', ' ')
    if decision_text:
        # 提取 "核心依据" 后的列表
        import re
        m = re.search(r'(?:核心依据|核心逻辑)[:：\s]*(.{50,300}?)(?:\n\n##|\n\*\*|执行策略)', decision_text, re.DOTALL)
        if m:
            return m.group(1).strip().replace('\n', ' ')
        # 备选: 提取最前面的 200 字符
        first_para = decision_text[:300].replace('\n', ' ')
        return first_para
    return "N/A"


def _extract_key_risks(decision_text: str) -> str:
    """从决策文本中提取关键风险."""
    import re
    if not decision_text:
        return "N/A"
    # 尝试找到 "风险" 或 "risk" 相关的小标题后的内容
    m = re.search(r'(?:关键风险|风险监控|Risk\s*Monitoring)[:：\s]*(.{30,300}?)(?:\n\n##|\n\*\*|总结)', decision_text, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        # 清理 markdown 列表符号
        lines = [re.sub(r'^[-*\s]+', '', l).strip() for l in raw.split('\n')]
        lines = [l for l in lines if l and not l.startswith('**')]
        return ' | '.join(lines[:4]) if lines else "N/A"
    # 备选: 从多个数字列表项中提取风险相关的
    risk_items = re.findall(r'\d+\.\s*(.{20,120}?)(?:风险|恶化|下行|质押|破产|退市)', decision_text)
    if risk_items:
        return ' | '.join(risk_items[:3])
    return "N/A"


def _extract_watch_points(decision_text: str) -> str:
    """从决策文本中提取关键观察节点."""
    import re
    if not decision_text:
        return "N/A"
    # 匹配 等待以下至少 / 观察节点 / 验证节点 等
    m = re.search(r'(?:观察节点|验证节点|等待以下|Watch|Monitor)[:：\s]*(.{30,300}?)(?:\n\n##|\n\*\*|总结|执行策略)', decision_text, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        # 去除前导空白和换行，保留列表结构
        lines = [l.strip() for l in raw.split('\n') if l.strip()]
        return ' | '.join(lines[:4])
    return "N/A"


def _extract_bull_bear_summary(investment_plan: str, decision_text: str) -> str:
    """从 investment_plan 或 decision_text 中提取多空摘要."""
    import re
    text = (investment_plan or "") + "\n" + (decision_text or "")
    if not text.strip():
        return "N/A"
    # 尝试提取多头/空头盘面描述
    m = re.search(r'(?:多空摘要|辩论裁定|空头|多头|Bull|Bear)[:：\s]*(.{40,250}?)(?:\n\n##|\n\*\*|执行策略)', text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip().replace('\n', ' ')
    # 备选: 从 investment_plan 的 Rationale 提取
    m = re.search(r'\*\*Rationale\*\*[:\s]*(.{40,250}?)(?:\n\n##|\n\*\*)', investment_plan, re.DOTALL)
    if m:
        return m.group(1).strip().replace('\n', ' ')
    # 备选: 从 decision_text 前面提取
    first_para = decision_text[:250].replace('\n', ' ') if decision_text else ""
    return first_para if first_para else "N/A"


# =====================================================================
# 汇总报告
# =====================================================================

def generate_summary(results: List[Dict[str, Any]], scan_time: float, portfolio_codes: Optional[set] = None) -> str:
    """生成扫描 + 分析汇总报告（结构化丰富版）."""
    lines = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 头部
    lines.append(f"\n{'='*70}")
    lines.append(" 📊 A-股市场扫描汇总报告")
    lines.append(f"{'='*70}")
    lines.append(f" 扫描时间: {now}")
    lines.append(f" 总耗时: {scan_time/60:.1f} 分钟")
    lines.append(f" 分析股票数: {len(results)}")
    lines.append("")

    # 评级分布
    rating_counts = {}
    for r in results:
        rt = r.get('analysis_result', {}).get('rating', 'UNKNOWN')
        rating_counts[rt] = rating_counts.get(rt, 0) + 1

    lines.append("📊 评级分布:")
    for rt in ['BUY', 'OVERWEIGHT', 'HOLD', 'UNDERWEIGHT', 'SELL', 'UNKNOWN', 'ERROR']:
        if rt in rating_counts:
            emoji = {'BUY': '🔴', 'OVERWEIGHT': '🟡', 'HOLD': '⚪', 'UNDERWEIGHT': '🟠', 'SELL': '🔴', 'UNKNOWN': '❓', 'ERROR': '❌'}.get(rt, '')
            lines.append(f"   {emoji} {rt:12s}: {rating_counts[rt]} 只")
    lines.append("")

    # 逐股深度摘要
    lines.append(f"{'='*70}")
    lines.append("📈 逐股深度摘要")
    lines.append(f"{'='*70}")
    lines.append("")

    for i, r in enumerate(results, 1):
        ar = r.get('analysis_result', {})
        rating = ar.get('rating', 'UNKNOWN')
        action = ar.get('action', 'Unknown')
        stop_loss = ar.get('stop_loss', 'N/A')
        price_target = ar.get('price_target', 'N/A')
        rationale = ar.get('rationale', 'N/A')
        key_risks = ar.get('key_risks', 'N/A')
        watch_points = ar.get('watch_points', 'N/A')
        bull_bear = ar.get('bull_bear_summary', 'N/A')
        report_file = ar.get('report_file', '')

        direction = "📈" if r['change_pct'] >= 0 else "📉"

        # 标题行
        portfolio_tag = "🏷️已持仓 " if portfolio_codes and r['code'] in portfolio_codes else ""
        lines.append(f"### {i}. {portfolio_tag}{r['name']} ({r['code']}) | 评级: **{rating}** | Action: **{action}**")
        lines.append(f"价格: {r['price']:.2f}  {direction}{r['change_pct']:+.2f}%  成交额: {r['turnover_million']:.0f}百万  主力流入: {r['main_inflow_million']:+.0f}百万")
        lines.append("")

        # 多空摘要
        if bull_bear and bull_bear != 'N/A':
            lines.append(f"**多空摘要**: {bull_bear[:200]}{'...' if len(bull_bear) > 200 else ''}")
            lines.append("")

        # 核心依据
        if rationale and rationale != 'N/A':
            lines.append(f"**核心依据**: {rationale[:250]}{'...' if len(rationale) > 250 else ''}")
            lines.append("")

        # 关键风险
        if key_risks and key_risks != 'N/A':
            lines.append(f"**关键风险**: {key_risks[:200]}{'...' if len(key_risks) > 200 else ''}")
            lines.append("")

        # 止损/目标价
        sl_pt = []
        if stop_loss and stop_loss != 'N/A':
            sl_pt.append(f"止损 {stop_loss}元")
        if price_target and price_target != 'N/A':
            sl_pt.append(f"目标价 {price_target}元")
        if sl_pt:
            lines.append(f"**止损/目标**: {' | '.join(sl_pt)}")
            lines.append("")

        # 观察节点
        if watch_points and watch_points != 'N/A':
            lines.append(f"**观察节点**: {watch_points[:200]}{'...' if len(watch_points) > 200 else ''}")
            lines.append("")

        # 完整报告路径
        if report_file:
            lines.append(f"**完整报告**: `{report_file}`")
            lines.append("")

        lines.append("")

    lines.append(f"{'='*70}")
    lines.append(f"✅ 汇总完成！共 {len(results)} 只股票分析。完整报告已保存至 stock_analysis_result/")
    lines.append(f"{'='*70}")

    summary = "\n".join(lines)
    return summary


# =====================================================================
# 主入口
# =====================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="A-Share Market Scanner")
    parser.add_argument("--strategy", choices=["conservative", "balanced", "aggressive"],
                        default="balanced", help="二级评分档位 (default: balanced)")
    parser.add_argument("--top-n", type=int, default=TOP_N, help=f"深度分析数量 (default: {TOP_N})")
    parser.add_argument("--skip-llm", action="store_true", help="跳过LLM分析，只输出量化筛选结果")
    args = parser.parse_args()

    start_time = time.time()
    print("="*70)
    print(" A-Share Market Scanner — 市场扫描 + LLM 分析")
    print("="*70)

    # Step 1: 扫描
    print(f"\n[阶段 1/2] 全市场扫描 (档位: {args.strategy})...")
    candidates = scan_market(strategy=args.strategy, top_n=args.top_n, skip_llm=args.skip_llm)

    # 如果 AkShare 失败，使用 WATCHLIST
    if not candidates and WATCHLIST:
        print(f"\n[WARN] 使用备选股票列表 ({len(WATCHLIST)} 只)")
        candidates = []
        for name in WATCHLIST:
            ticker, cname = resolve_ticker(name)
            code = ticker.split('.')[0]
            candidates.append({
                'code': code,
                'name': cname,
                'price': 0,
                'change_pct': 0,
                'turnover': 0,
                'turnover_million': 0,
                'rsi': None,
                'sentiment_score': None,
                'score': 0,
            })

    if not candidates:
        print("[ERROR] 没有找到合适的候选股票，请检查筛选条件或添加 WATCHLIST")
        return

    print(f"\n[成功] 找到 {len(candidates)} 只候选股票：")
    for i, c in enumerate(candidates, 1):
        direction = "涨" if c['change_pct'] >= 0 else "跌"
        print(f"  {i}. {c['name']} ({c['code']})  {direction}:{c['change_pct']:+.2f}%  成交额:{c['turnover_million']:.0f}百万  主力流入:{c['main_inflow_million']:+.0f}百万  综合分:{c['score']}")

    # Step 2: LLM 分析 (选股阶段使用 shallow 模式提速)
    if args.skip_llm:
        print("\n[INFO] --skip-llm 已设置，跳过 LLM 分析")
        results = candidates
    else:
        print(f"\n[阶段 2/2] 对 Top {len(candidates)} 逐一分析 (模式: shallow，每只约 2-3 分钟)...")
        results = analyze_candidates(candidates, mode="shallow")

    # Step 3: 汇总
    total_time = time.time() - start_time
    portfolio_codes = _load_portfolio_codes()
    summary = generate_summary(results, total_time, portfolio_codes)
    print(summary)

    # 保存汇总报告 + 候选结果JSON
    from pathlib import Path
    results_dir = Path("stock_analysis_result")
    results_dir.mkdir(exist_ok=True)
    summary_file = results_dir / f"scan_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(summary)
    print(f"\n📄 汇总报告已保存: {summary_file}")

    # 保存候选结果JSON（供日常扫描脚本读取推荐理由）
    candidates_json = results_dir / f"scan_candidates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    # 从浅度分析后的 results 导出，包含分析结论
    export_candidates = []
    for c in results:
        ar = c.get('analysis_result', {})
        decision = ar.get('decision', '')
        # 截断分析结论到150字符作为推荐理由
        analysis_reason = (decision[:150] + "...") if len(decision) > 150 else decision
        export_candidates.append({
            'code': c.get('code'),
            'name': c.get('name'),
            'price': c.get('price'),
            'change_pct': c.get('change_pct'),
            'turnover_million': c.get('turnover_million'),
            'main_inflow_million': c.get('main_inflow_million'),
            'rsi': c.get('rsi'),
            'sentiment_score': c.get('sentiment_score'),
            'score': c.get('score'),
            'score_details': c.get('score_details', []),
            'analysis_reason': analysis_reason,
            'in_portfolio': c.get('code') in portfolio_codes if portfolio_codes else False,
        })
    with open(candidates_json, 'w', encoding='utf-8') as f:
        json.dump(export_candidates, f, ensure_ascii=False, indent=2)
    print(f"📄 候选结果JSON已保存: {candidates_json}")
    # 同时更新 latest 链接
    latest_json = results_dir / "scan_candidates_latest.json"
    with open(latest_json, 'w', encoding='utf-8') as f:
        json.dump(export_candidates, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
