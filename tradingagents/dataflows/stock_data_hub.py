"""Stock Data Hub vendor adapter for TradingAgents.

Wraps westock-data (npx CLI) + optional MX to provide data
through the TradingAgents vendor routing system.
"""

import os, re, subprocess
from datetime import datetime, date, timedelta
import pandas as pd

WESTOCK_VERSION = "1.0.3"
USE_MX_FIRST = os.environ.get("STOCK_DATA_HUB_MX_FIRST", "1") == "1"
MX_COLUMN_MAP = {
    "\u65e5\u671f": "Date", "date": "Date",
    "\u5f00\u76d8\u4ef7": "Open", "open": "Open",
    "\u6536\u76d8\u4ef7": "Close", "close": "Close",
    "\u6700\u9ad8\u4ef7": "High", "high": "High",
    "\u6700\u4f4e\u4ef7": "Low", "low": "Low",
    "\u6210\u4ea4\u91cf": "Volume", "volume": "Volume",
    "\u6210\u4ea4\u989d": "Amount", "amount": "Amount",
}

def _mx_enabled():
    if not USE_MX_FIRST or not os.environ.get("MX_APIKEY"):
        return False
    try:
        import sys
        from pathlib import Path
        home = str(Path.home())
        for p in [f"{home}/mx-data", f"{home}/mx-search"]:
            if p not in sys.path:
                sys.path.insert(0, p)
        import mx_data, mx_search
        return True
    except ImportError:
        return False

def _mx_data_query_safe(query, timeout=20):
    try:
        import sys
        from pathlib import Path
        home = str(Path.home())
        for p in [f"{home}/mx-data", f"{home}/mx-search"]:
            if p not in sys.path:
                sys.path.insert(0, p)
        import mx_data
        client = mx_data.MXData()
        result = client.query(query)
        if result.get("status") != 0:
            return None, None
        tables, _, total_rows, error = mx_data.MXData.parse_result(result)
        if error or not tables:
            return None, None
        return tables, total_rows
    except Exception:
        return None, None

def _mx_search_query_safe(query, timeout=20):
    try:
        import sys
        from pathlib import Path
        home = str(Path.home())
        for p in [f"{home}/mx-data", f"{home}/mx-search"]:
            if p not in sys.path:
                sys.path.insert(0, p)
        import mx_search
        client = mx_search.MXSearch()
        result = client.search(query)
        if result.get("status") != 0:
            return None
        content = mx_search.MXSearch.extract_content(result)
        return content if content else None
    except Exception:
        return None

def _mx_tables_to_df(tables):
    if not tables:
        return pd.DataFrame()
    t = tables[0]
    rows = t.get("rows", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    rename_map = {col: MX_COLUMN_MAP.get(col, col) for col in df.columns
                  if MX_COLUMN_MAP.get(col, col) != col}
    if rename_map:
        df = df.rename(columns=rename_map)
    return df

def _ticker_to_name(ticker):
    return ticker.replace(".SS","").replace(".SZ","").replace(".BJ","")

def _westock_cmd(args, timeout=30):
    cmd = ["npx", "-y", f"westock-data-skillhub@{WESTOCK_VERSION}"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"westock-data failed: {result.stderr[:300]}")
    return result.stdout

def _markdown_table_to_df(text):
    lines = text.strip().split("\n")
    table_lines = [l for l in lines if l.strip().startswith("|")]
    if len(table_lines) < 2:
        return pd.DataFrame()
    headers = [h.strip() for h in table_lines[0].split("|")[1:-1]]
    data = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) == len(headers):
            data.append(cells)
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data, columns=headers)

def _md_tables_to_dict(text):
    sections = {}
    current_key = None
    current_lines = []
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r'\*\*(.+?)\*\*', s)
        if m and not s.startswith("|"):
            if current_key and current_lines:
                sections[current_key] = _markdown_table_to_df("\n".join(current_lines))
            current_key = m.group(1).strip()
            current_lines = []
        elif current_key is not None:
            if s.startswith("|") or s == "":
                current_lines.append(line)
    if current_key and current_lines:
        sections[current_key] = _markdown_table_to_df("\n".join(current_lines))
    return sections

def _to_ws(symbol):
    symbol = symbol.strip().upper()
    for p in ("sh", "sz", "bj", "hk", "us"):
        if symbol.lower().startswith(p):
            return symbol.lower()
    if symbol.isalpha():
        return f"us{symbol.lower()}"
    if ".SS" in symbol:
        return "sh" + symbol.replace(".SS","").lower()
    if ".SZ" in symbol:
        return "sz" + symbol.replace(".SZ","").lower()
    if ".BJ" in symbol:
        return "bj" + symbol.replace(".BJ","").lower()
    if symbol.isdigit() and len(symbol) == 6:
        return ("sh" if symbol.startswith(("6","9")) else "sz") + symbol
    return symbol.lower()

def get_stock(symbol, start_date, end_date):
    if _mx_enabled():
        try:
            code = _ticker_to_name(symbol)
            q = f"{code}\u4ece{start_date}\u5230{end_date}\u7684\u65e5K\u7ebf\u6570\u636e"
            tables, total = _mx_data_query_safe(q)
            if tables and total:
                df = _mx_tables_to_df(tables)
                if not df.empty:
                    h = f"# Stock data for {symbol.upper()} from {start_date} to {end_date} (via MX)\n"
                    return h + f"# Total: {len(df)}\n\n" + df.to_csv(index=False)
        except Exception:
            pass

    ws = _to_ws(symbol)
    try:
        try:
            days = min((datetime.now() - datetime.strptime(start_date,"%Y-%m-%d")).days + 30, 2000)
        except:
            days = 500
        output = _westock_cmd(["kline", ws, "--period", "day", "--limit", str(days), "--fq", "qfq"])
        df = _markdown_table_to_df(output)
        if df.empty:
            return f"No data for {symbol} {start_date}~{end_date}\n"
        rename = {"date":"Date","open":"Open","last":"Close","high":"High","low":"Low","volume":"Volume","amount":"Amount"}
        df = df.rename(columns={k:v for k,v in rename.items() if k in df.columns})
        if "Date" in df.columns:
            df = df[(df["Date"]>=start_date)&(df["Date"]<=end_date)]
        h = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
        return h + f"# Total: {len(df)}\n\n" + df.to_csv(index=False)
    except Exception as e:
        return f"Error: {symbol}: {str(e)[:200]}\n"

def get_indicator(symbol, indicator, curr_date, look_back_days=30):
    if _mx_enabled():
        try:
            code = _ticker_to_name(symbol)
            q = f"{code}\u7684{indicator}\u6280\u672f\u6307\u6807"
            tables, total = _mx_data_query_safe(q)
            if tables and total:
                df = _mx_tables_to_df(tables)
                if not df.empty:
                    lines = [f"## {indicator.upper()} for {symbol.upper()} (via MX)\n"]
                    for _, row in df.iterrows():
                        dv = row.get("Date","")
                        for c in df.columns:
                            if c!="Date" and pd.notna(row.get(c)):
                                try: lines.append(f"{dv}: {float(row[c]):.2f}")
                                except: pass
                                break
                    return "\n".join(lines) + "\n"
        except Exception:
            pass

    ws = _to_ws(symbol)
    gmap = {"macd":"macd","macds":"macd","rsi":"rsi","boll":"boll","kdj":"kdj"}
    group = gmap.get(indicator.lower(), "all")
    try:
        output = _westock_cmd(["technical", ws, "--group", group])
        df = _markdown_table_to_df(output)
        if df.empty:
            return f"No technical data for {symbol}\n"
        cmap = {"macd":"macd.MACD","rsi":"rsi.RSI_6","boll":"boll.BOLL_MID",
                "close_50_sma":"ma.MA_50","close_200_sma":"ma.MA_200"}
        col = cmap.get(indicator.lower(), "")
        lines = [f"## {indicator.upper()} for {symbol.upper()}\n"]
        for _, row in df.iterrows():
            dv = row.get("date","")
            val = row.get(col) if col and col in df.columns else None
            if val is None:
                for c in df.columns:
                    if indicator.lower() in c.lower() and c!="date":
                        val = row.get(c); break
            if dv and val is not None and str(val) not in ("-",""):
                try: lines.append(f"{dv}: {float(val):.2f}")
                except: lines.append(f"{dv}: {val}")
        return "\n".join(lines) + "\n" if len(lines)>1 else "No indicator values found.\n"
    except Exception as e:
        return f"Error indicators {symbol}: {str(e)[:200]}\n"

def get_fundamentals(ticker, curr_date=None):
    if _mx_enabled():
        try:
            code = _ticker_to_name(ticker)
            q = f"{code}\u7684\u516c\u53f8\u57fa\u672c\u9762\u6570\u636e"
            tables, total = _mx_data_query_safe(q)
            if tables and total:
                df = _mx_tables_to_df(tables)
                if not df.empty:
                    lines = [f"# Fundamentals for {ticker.upper()} (via MX)", ""]
                    for _, row in df.iterrows():
                        for c in df.columns:
                            lines.append(f"{c}: {row.get(c,'N/A')}")
                        lines.append("")
                    return "\n".join(lines)
        except Exception:
            pass

    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["profile", ws])
        df = _markdown_table_to_df(output)
        if df.empty:
            return f"No fundamentals for {ticker}\n"
        lines = [f"# Fundamentals for {ticker.upper()}", ""]
        for _, row in df.iterrows():
            for c in df.columns:
                lines.append(f"{c}: {row.get(c,'N/A')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fundamentals {ticker}: {str(e)[:200]}\n"

def get_balance_sheet(ticker, freq="quarterly", curr_date=None):
    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["finance", ws, "--num", "1"])
        sections = _md_tables_to_dict(output)
        df = sections.get("zcfz", pd.DataFrame())
        h = f"# Balance Sheet for {ticker.upper()} ({freq})\n"
        return h + (df.to_csv(index=False) if not df.empty else "No data.\n")
    except Exception as e:
        return f"# Balance Sheet for {ticker.upper()}\n\nError: {e}\n"

def get_cashflow(ticker, freq="quarterly", curr_date=None):
    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["finance", ws, "--num", "1"])
        sections = _md_tables_to_dict(output)
        df = sections.get("xjll", pd.DataFrame())
        h = f"# Cash Flow for {ticker.upper()} ({freq})\n"
        return h + (df.to_csv(index=False) if not df.empty else "No data.\n")
    except Exception as e:
        return f"# Cash Flow for {ticker.upper()}\n\nError: {e}\n"

def get_income_statement(ticker, freq="quarterly", curr_date=None):
    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["finance", ws, "--num", "1"])
        sections = _md_tables_to_dict(output)
        df = sections.get("lrb", pd.DataFrame())
        h = f"# Income Statement for {ticker.upper()} ({freq})\n"
        return h + (df.to_csv(index=False) if not df.empty else "No data.\n")
    except Exception as e:
        return f"# Income Statement for {ticker.upper()}\n\nError: {e}\n"

def get_news(ticker, start_date, end_date):
    if _mx_enabled():
        try:
            content = _mx_search_query_safe(f"{_ticker_to_name(ticker)}\u4ece{start_date}\u5230{end_date}\u7684\u65b0\u95fb")
            if content:
                return f"## {ticker.upper()} News {start_date}~{end_date} (via MX)\n\n{content}\n"
        except Exception:
            pass
    return f"## {ticker.upper()} News {start_date}~{end_date}\n\nNews not available via westock-data.\n\n"

def get_global_news(curr_date, look_back_days=7, limit=50):
    if _mx_enabled():
        try:
            content = _mx_search_query_safe(f"\u5168\u7403\u5e02\u573a\u6700\u8fd1{look_back_days}\u5929\u7684\u91cd\u8981\u65b0\u95fb")
            if content:
                return f"## Global News (via MX)\n\n{content}\n"
        except Exception:
            pass
    return "## Global News\n\nNot available via westock-data.\n\n"

def get_insider_transactions(ticker):
    return f"# Insider Transactions for {ticker.upper()}\n\nNot available via stock_data_hub.\n"

def get_capital_flow(ticker):
    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["asfund", ws])
        df = _markdown_table_to_df(output)
        if df is None or df.empty:
            return f"# Capital Flow for {ticker.upper()}\n\nNo data.\n"
        return f"# Capital Flow for {ticker.upper()}\n\n{df.to_csv(index=False)}"
    except Exception as e:
        return f"# Capital Flow for {ticker.upper()}\n\nError: {e}\n"

def get_margin_data(ticker):
    ws = _to_ws(ticker)
    try:
        output = _westock_cmd(["margintrade", ws])
        df = _markdown_table_to_df(output)
        if df is None or df.empty:
            return f"# Margin Trading for {ticker.upper()}\n\nNo data.\n"
        return f"# Margin Trading for {ticker.upper()}\n\n{df.to_csv(index=False)}"
    except Exception as e:
        return f"# Margin Trading for {ticker.upper()}\n\nError: {e}\n"
