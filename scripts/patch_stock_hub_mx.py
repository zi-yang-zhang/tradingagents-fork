#!/usr/bin/env python3
"""将妙想skill(MX)集成到stock_data_hub.py，作为首选数据源，fallback到stock-data-hub
"""

from pathlib import Path

SRC = Path("/home/ubuntu/tradingagents-fork/tradingagents/dataflows/stock_data_hub.py")
content = SRC.read_text()

# =====================================================================
# 1. 添加 import os 和 MX 配置区
# =====================================================================
mx_config_block = '''import os

# =====================================================================
# MX (妙想) 优先配置
# =====================================================================
USE_MX_FIRST = os.environ.get("STOCK_DATA_HUB_MX_FIRST", "1") == "1"
"""优先使用妙想skill获取数据，失败时自动fallback到stock-data-hub"""

MX_COLUMN_MAP = {
    # 中文/英文列名 → 标准列名
    "日期": "Date", "date": "Date", "Date": "Date", "时间": "Date", "trade_date": "Date",
    "开盘价": "Open", "open": "Open", "Open": "Open", "开盘": "Open",
    "收盘价": "Close", "close": "Close", "Close": "Close", "收盘": "Close",
    "最高价": "High", "high": "High", "High": "High", "最高": "High",
    "最低价": "Low", "low": "Low", "Low": "Low", "最低": "Low",
    "成交量": "Volume", "volume": "Volume", "Volume": "Volume", "成交": "Volume",
    "成交额": "Amount", "amount": "Amount", "Amount": "Amount",
    "涨跌幅": "ChangePct", "涨跌幅(%)": "ChangePct", "涨跌": "ChangePct",
    "换手率": "Turnover", "换手率(%)": "Turnover",
}


def _mx_enabled() -> bool:
    """检查MX是否可用（环境变量已设置且模块可导入）"""
    if not USE_MX_FIRST:
        return False
    if not os.environ.get("MX_APIKEY"):
        return False
    try:
        import mx_data
        import mx_search
        return True
    except ImportError:
        return False


def _mx_data_query_safe(query: str, timeout: int = 20):
    """安全调用MX数据查询，返回(tables, total_rows)或(None, None)"""
    try:
        import mx_data
        client = mx_data.MXData()
        result = client.query(query)
        status = result.get("status")
        if status != 0:
            return None, None
        tables, _, total_rows, error = mx_data.MXData.parse_result(result)
        if error or not tables:
            return None, None
        return tables, total_rows
    except Exception:
        return None, None


def _mx_tables_to_df(tables: list) -> "pd.DataFrame":
    """将MX返回的tables转换为DataFrame，并做列名标准化"""
    if not tables:
        return pd.DataFrame()
    t = tables[0]
    rows = t.get("rows", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # 列名标准化
    rename_map = {}
    for col in df.columns:
        std = MX_COLUMN_MAP.get(col, col)
        if std != col:
            rename_map[col] = std
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _mx_search_query_safe(query: str, timeout: int = 20) -> Optional[str]:
    """安全调用MX搜索，返回文本内容或None"""
    try:
        import mx_search
        client = mx_search.MXSearch()
        result = client.search(query)
        status = result.get("status")
        if status != 0:
            return None
        content = mx_search.MXSearch.extract_content(result)
        return content if content else None
    except Exception:
        return None


def _ticker_to_mx_query(ticker: str) -> str:
    """将TradingAgents ticker转换为MX查询用的股票名称/代码"""
    ticker = ticker.strip()
    if "." in ticker:
        code = ticker.split(".")[0]
    else:
        code = ticker
    if code.isdigit() and len(code) == 6:
        return code  # 6位代码，MX直接支持
    if code.isalpha():
        return code  # 美股/港股代码
    return code

'''

# 插入到 from .config import get_config 之后
marker = "from .config import get_config\n"
if marker in content:
    content = content.replace(marker, marker + "\n" + mx_config_block)

# =====================================================================
# 2. 修改 get_stock 添加 MX-first
# =====================================================================
old_get_stock_body = '''    hub_symbol = _to_stock_hub_symbol(symbol)
    hub = _get_hub()

    # Parse dates
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    # Calculate limit - need enough data to cover the date range
    # stock-data-hub returns latest N records, so we need a large limit
    # to ensure historical data is included
    if start_dt and end_dt:
        days = (datetime.now().date() - start_dt).days
        limit = max(days + 100, 500)  # buffer for weekends/holidays
    else:
        limit = 500

    df = hub.get_kline('''

new_get_stock_body = '''    # --- MX 妙想优先 ---
    if _mx_enabled():
        try:
            name = _ticker_to_mx_query(symbol)
            sd = start_date or ""
            ed = end_date or ""
            query = f"{name} {sd}到{ed} 每天的开盘价 收盘价 最高价 最低价 成交量"
            tables, total_rows = _mx_data_query_safe(query)
            if tables:
                df = _mx_tables_to_df(tables)
                if not df.empty and "Date" in df.columns:
                    # 过滤日期范围
                    if start_date:
                        df = df[df["Date"] >= start_date]
                    if end_date:
                        df = df[df["Date"] <= end_date]
                    if not df.empty:
                        header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\\n"
                        header += f"# Total records: {len(df)}\\n"
                        header += f"# Data source: 妙惲(MX) → 东方财富权威数据\\n"
                        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\\n\\n"
                        return header + df.to_csv(index=False)
        except Exception:
            pass  # fallback to stock-data-hub

    hub_symbol = _to_stock_hub_symbol(symbol)
    hub = _get_hub()

    # Parse dates
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    # Calculate limit - need enough data to cover the date range
    # stock-data-hub returns latest N records, so we need a large limit
    # to ensure historical data is included
    if start_dt and end_dt:
        days = (datetime.now().date() - start_dt).days
        limit = max(days + 100, 500)  # buffer for weekends/holidays
    else:
        limit = 500

    df = hub.get_kline('''

content = content.replace(old_get_stock_body, new_get_stock_body)

# =====================================================================
# 3. 修改 get_indicator 添加 MX-first
# =====================================================================
old_indicator_start = '''    hub_symbol = _to_stock_hub_symbol(symbol)
    hub = _get_hub()

    # Map indicator names to stock-data-hub groups
    group_map = {'''

new_indicator_start = '''    # --- MX 妙惲优先 ---
    if _mx_enabled():
        try:
            name = _ticker_to_mx_query(symbol)
            query = f"{name} {indicator} 技术指标"
            tables, total_rows = _mx_data_query_safe(query)
            if tables:
                df = _mx_tables_to_df(tables)
                if not df.empty:
                    header = f"## {indicator.upper()} values for {symbol}\\n\\n"
                    header += f"# Data source: 妙惲(MX) → 东方财富权威数据\\n\\n"
                    # 输出表格内容
                    lines = []
                    date_col = next((c for c in df.columns if "date" in c.lower()), None)
                    val_col = next((c for c in df.columns if indicator.lower() in c.lower() and c != date_col), None)
                    if not val_col and len(df.columns) > 1:
                        val_col = [c for c in df.columns if c != date_col][0]
                    for _, row in df.iterrows():
                        d = row.get(date_col, "") if date_col else ""
                        v = row.get(val_col, "") if val_col else ""
                        if d:
                            lines.append(f"{d}: {v}")
                    return header + "\\n".join(lines) + f"\\n\\nIndicator: {indicator} (source: MX)"
        except Exception:
            pass

    hub_symbol = _to_stock_hub_symbol(symbol)
    hub = _get_hub()

    # Map indicator names to stock-data-hub groups
    group_map = {'''

content = content.replace(old_indicator_start, new_indicator_start)

# =====================================================================
# 4. 修改 get_fundamentals 添加 MX-first
# =====================================================================
old_fund_start = '''    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    # Get company profile
    profile_df = hub.get_profile(hub_symbol)'''

new_fund_start = '''    # --- MX 妙惲优先 ---
    if _mx_enabled():
        try:
            name = _ticker_to_mx_query(ticker)
            query = f"{name} 最近4期财务报表 利润表 资产负债表 现金流量表"
            tables, total_rows = _mx_data_query_safe(query)
            if tables:
                lines = [f"# Company Fundamentals for {ticker.upper()}", "", "# Data source: 妙惲(MX) → 东方财富权威数据", ""]
                for t in tables:
                    sheet = t.get("sheet_name", "Unknown")
                    lines.append(f"## {sheet}")
                    rows = t.get("rows", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        lines.append(df.to_csv(index=False))
                    lines.append("")
                lines.append(f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                return "\\n".join(lines)
        except Exception:
            pass

    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    # Get company profile
    profile_df = hub.get_profile(hub_symbol)'''

content = content.replace(old_fund_start, new_fund_start)

# =====================================================================
# 5. 修改 get_news 添加 MX-first
# =====================================================================
old_news_start = '''    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    df = hub.get_stock_news('''

new_news_start = '''    # --- MX 妙惲优先 ---
    if _mx_enabled():
        try:
            name = _ticker_to_mx_query(ticker)
            query = f"{name} 最新新闻"
            content = _mx_search_query_safe(query)
            if content:
                header = f"## {ticker} News, from {start_date} to {end_date}:\\n\\n"
                header += f"# Data source: 妙惲(MX) → 东方财富资讯搜索\\n\\n"
                return header + content
        except Exception:
            pass

    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    df = hub.get_stock_news('''

content = content.replace(old_news_start, new_news_start)

# =====================================================================
# 6. 修改 get_global_news 添加 MX-first
# =====================================================================
old_global_news = '''    hub = _get_hub()

    # Handle None values from LLM tool calls
    if look_back_days is None:
        look_back_days = 7
    if limit is None:
        limit = 50

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d").date()'''

new_global_news = '''    # --- MX 妙惲优先 ---
    if _mx_enabled():
        try:
            query = "最新市场行情 重大新闻"
            content = _mx_search_query_safe(query)
            if content:
                header = f"## Global Market News, from {curr_date} (lookback {look_back_days} days):\\n\\n"
                header += f"# Data source: 妙惲(MX) → 东方财富资讯搜索\\n\\n"
                return header + content
        except Exception:
            pass

    hub = _get_hub()

    # Handle None values from LLM tool calls
    if look_back_days is None:
        look_back_days = 7
    if limit is None:
        limit = 50

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d").date()'''

content = content.replace(old_global_news, new_global_news)

# =====================================================================
# 7. 修改 get_sentiment_data 添加 MX-first
# =====================================================================
old_sentiment = '''    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    # 获取最新情绪摘要
    summary_df = hub.get_sentiment_summary(hub_symbol)'''

new_sentiment = '''    # --- MX 妙惲优先 ---
    if _mx_enabled():
        try:
            name = _ticker_to_mx_query(ticker)
            query = f"{name} 千股千评 情绪分析"
            tables, total_rows = _mx_data_query_safe(query)
            if tables:
                lines = [f"# A-Share Sentiment Data for {ticker.upper()}", "", "# Data source: 妙惲(MX) → 东方财富权威数据", ""]
                for t in tables:
                    sheet = t.get("sheet_name", "Unknown")
                    lines.append(f"## {sheet}")
                    rows = t.get("rows", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        lines.append(df.to_csv(index=False))
                    lines.append("")
                lines.append(f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                return "\\n".join(lines)
        except Exception:
            pass

    hub_symbol = _to_stock_hub_symbol(ticker)
    hub = _get_hub()

    # 获取最新情绪摘要
    summary_df = hub.get_sentiment_summary(hub_symbol)'''

content = content.replace(old_sentiment, new_sentiment)

# 写回
SRC.write_text(content)
print("✅ stock_data_hub.py 已更新，MX-first fallback 逻辑已注入")

# 语法检查
import py_compile
try:
    py_compile.compile(str(SRC), doraise=True)
    print("✅ 语法检查通过")
except py_compile.PyCompileError as e:
    print(f"❌ 语法错误: {e}")
    raise
