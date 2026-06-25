#!/usr/bin/env python3
"""妙想金融 Skills MCP Server

将东方财富妙想5个 Skill 封装为 MCP Server，通过 stdio transport 供 Hermes Agent 调用:
- mx-data:     金融数据查询
- mx-search:   资讯搜索
- mx-xuangu:   智能选股
- mx-zixuan:   自选股管理
- mx-moni:     模拟组合管理

环境变量: MX_APIKEY (必需)
"""

import sys
import os
import json
from pathlib import Path

# 添加妙想 skill 路径
sys.path.insert(0, str(Path.home() / "mx-data"))
sys.path.insert(0, str(Path.home() / "mx-search"))
sys.path.insert(0, str(Path.home() / "mx-xuangu"))
sys.path.insert(0, str(Path.home() / "mx-zixuan"))
sys.path.insert(0, str(Path.home() / "mx-moni"))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mx-skills")

# =====================================================================
# Tool 1: mx_data_query 金融数据查询
# =====================================================================
@mcp.tool()
def mx_data_query(query: str) -> str:
    """查询金融数据，支持行情、财务、关联关系等数据。

Args:
    query: 自然语言查询问句，如 "东方财富最新价" 、 "茅台近三年净利润"
"""
    try:
        from mx_data import MXData
        client = MXData()
        result = client.query(query)

        status = result.get("status")
        if status != 0:
            msg = result.get("message", "")
            return f"❌ 查询失败: 状态码 {status} - {msg}"

        tables, condition_parts, total_rows, error = MXData.parse_result(result)
        if error:
            return f"⚠️ 解析结果异常: {error}\n\n原始响应:\n{json.dumps(result, ensure_ascii=False, indent=2)[:500]}"

        output = [f"✅ 查询成功，共 {total_rows} 条数据"]
        if condition_parts:
            output.append("")
            output.append("📋 查询条件:")
            for cp in condition_parts:
                output.append(cp)

        for t in tables:
            output.append("")
            output.append(f"📊 {t['sheet_name']} ({len(t['rows'])} 行)")
            output.append("-" * 50)
            # 输出表头
            headers = t.get("fieldnames", [])
            if headers:
                output.append(" | ".join(headers[:8]))  # 最多8列
            # 输出前5行
            for i, row in enumerate(t["rows"][:5]):
                vals = [str(row.get(h, "")) for h in headers[:8]]
                output.append(" | ".join(vals))
            if len(t["rows"]) > 5:
                output.append(f"... 等共 {len(t['rows'])} 行")

        return "\n".join(output)
    except Exception as e:
        return f"❌ 调用失败: {type(e).__name__}: {e}"


# =====================================================================
# Tool 2: mx_search_query 资讯搜索
# =====================================================================
@mcp.tool()
def mx_search_query(query: str) -> str:
    """搜索金融资讯、研报、新闻。

Args:
    query: 搜索问句，如 "格力电器最新研报" 、 "比亚迪重大利好"
"""
    try:
        from mx_search import MXSearch
        client = MXSearch()
        result = client.search(query)

        status = result.get("status")
        if status != 0:
            msg = result.get("message", "")
            return f"❌ 搜索失败: 状态码 {status} - {msg}"

        content = MXSearch.extract_content(result)
        if not content:
            return f"✅ 搜索成功，但未返回文本内容。\n\n原始响应:\n{json.dumps(result, ensure_ascii=False, indent=2)[:800]}"

        return f"✅ 搜索结果:\n\n{content}"
    except Exception as e:
        return f"❌ 调用失败: {type(e).__name__}: {e}"


# =====================================================================
# Tool 3: mx_xuangu_query 智能选股
# =====================================================================
@mcp.tool()
def mx_xuangu_query(query: str) -> str:
    """通过自然语言进行智能选股，支持 A股/港股/美股/板块/基金/ETF。

Args:
    query: 选股条件，如 "股价大于10元的A股" 、 "近一周涨幅超过5%的科技股"
"""
    try:
        from mx_xuangu import MXSelectStock
        client = MXSelectStock()
        result = client.search(query)

        status = result.get("status")
        if status != 0:
            msg = result.get("message", "")
            return f"❌ 选股失败: 状态码 {status} - {msg}"

        rows, data_source, error = MXSelectStock.extract_data(result)
        if error:
            return f"⚠️ 解析结果异常: {error}\n\n原始响应:\n{json.dumps(result, ensure_ascii=False, indent=2)[:500]}"

        if not rows:
            return f"✅ 选股成功，但未找到匹配结果。\n\n原始响应:\n{json.dumps(result, ensure_ascii=False, indent=2)[:800]}"

        output = [f"✅ 选股结果 (数据源: {data_source}, 共 {len(rows)} 只)"]
        output.append("")

        # 输出表头
        if rows:
            headers = list(rows[0].keys())[:8]
            output.append(" | ".join(headers))
            output.append("-" * 60)

        for i, row in enumerate(rows[:10]):
            vals = [str(row.get(h, "")) for h in headers]
            output.append(" | ".join(vals))

        if len(rows) > 10:
            output.append(f"... 等共 {len(rows)} 只")

        return "\n".join(output)
    except Exception as e:
        return f"❌ 调用失败: {type(e).__name__}: {e}"


# =====================================================================
# Tool 4: mx_zixuan_query 自选股管理
# =====================================================================
@mcp.tool()
def mx_zixuan_query(query: str) -> str:
    """自选股查询与管理。支持查询自选列表、添加股票、删除股票。

Args:
    query: 操作指令，如 "查询我的自选" 、 "添加茅台赋于自选" 、 "删除比亚迪"
"""
    try:
        from mx_zixuan import get_apikey, query_self_select, manage_self_select

        apikey = get_apikey()
        query_lower = query.lower()

        # 判断是查询还是管理操作
        query_keywords = ["查询", "列表", "我的自选", "有哪些"]
        is_query = any(kw in query_lower for kw in query_keywords)

        if is_query:
            result = query_self_select(apikey)
            status = result.get("status")
            if status != 0:
                return f"❌ 查询失败: {result.get('message', '')}"

            data = result.get("data", {})
            inner = data.get("data", {})
            stock_list = inner.get("stockList", [])

            if not stock_list:
                return "✅ 自选列表为空"

            output = [f"✅ 自选列表 (共 {len(stock_list)} 只)"]
            output.append("")
            for s in stock_list:
                name = s.get("name", "")
                code = s.get("code", "")
                market = s.get("market", "")
                output.append(f"  {name} ({code}) [{market}]")
            return "\n".join(output)
        else:
            result = manage_self_select(apikey, query)
            status = result.get("status")
            if status != 0:
                return f"❌ 操作失败: {result.get('message', '')}"

            data = result.get("data", {})
            inner = data.get("data", {})
            msg = inner.get("message", "操作完成")
            return f"✅ {msg}"
    except Exception as e:
        return f"❌ 调用失败: {type(e).__name__}: {e}"


# =====================================================================
# Tool 5: mx_moni_query 模拟组合管理
# =====================================================================
@mcp.tool()
def mx_moni_query(query: str) -> str:
    """模拟组合管理，支持查询持仓、资金、委托、买卖、撚单等操作。

Args:
    query: 操作指令，如 "查询我的持仓" 、 "买入 000001 100股" 、 "一键撚单"
"""
    try:
        import mx_moni

        intent = mx_moni.parse_query(query)
        if not intent:
            return "⚠️ 无法识别操作意图，请使用明确的关键词，如: '持仓'/'资金'/'买入'/'卖出'/'撤单'/'委托'"

        apikey = os.environ.get('MX_APIKEY', '')
        if not apikey:
            return "❌ MX_APIKEY 未设置"

        # 构建 payload
        if intent in ('buy', 'sell'):
            trade_info = mx_moni.extract_trade_info(query, intent)
            payload = {
                "action": intent,
                **trade_info
            }
        elif intent == 'cancel':
            payload = {"action": "cancel"}
        elif intent == 'positions':
            payload = {"action": "positions"}
        elif intent == 'balance':
            payload = {"action": "balance"}
        elif intent == 'orders':
            payload = {"action": "orders"}
        elif intent == 'newPost':
            payload = {"action": "newPost", "content": query}
        else:
            payload = {"action": intent, "query": query}

        result = mx_moni.api_request("/api/claw/portfolio/trade", payload)
        if result is None:
            return "❌ API 请求失败"

        formatted = mx_moni.format_result(intent, result)
        return formatted
    except Exception as e:
        return f"❌ 调用失败: {type(e).__name__}: {e}"


# =====================================================================
# 运行
# =====================================================================
if __name__ == "__main__":
    # 检查环境变量
    if not os.environ.get("MX_APIKEY"):
        print("ERROR: MX_APIKEY 环境变量未设置", file=sys.stderr)
        sys.exit(1)

    mcp.run(transport="stdio")
