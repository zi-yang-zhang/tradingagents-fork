#!/usr/bin/env python3
"""持仓截图解析器 — 将大模型 vision 解析后的持仓文本转换为标准 JSON

使用流程（推荐）:
  1. 用户发送持仓截图
  2. Agent 用 vision 大模型解析截图，提取结构化文本
  3. 将文本保存为 .txt 文件
  4. 调用本脚本: python scripts/holding_screenshot_parser.py --text /path/to/parsed.txt
  5. 输出: ~/.hermes/stock_holdings.json (覆盖写入)

支持的输入（但推荐 --text）:
  --text  : 大模型 vision 解析后的文本文件（推荐方式）
  --image : 截图图片路径（不推荐，会提示转用 --text）

用法:
  python scripts/holding_screenshot_parser.py --text /tmp/holding_parsed.txt
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

# ── 默认路径 ──────────────────────────────────────
DEFAULT_HOLDINGS_PATH = Path.home() / ".hermes" / "stock_holdings.json"

# 股票名称 → 代码映射 (截图中通常只显示名称，无代码)
# 后续可通过 akshare 动态扩展，这里用静态表保证可靠性
NAME_TO_CODE: Dict[str, str] = {
    "TCL科技": "000100",
    "五粮液": "000858",
    "东山精密": "002384",
    "立讯精密": "002475",
    "领益智造": "002600",
    "鹏鼎控股": "002938",
    "亿纬锂能": "300014",
    "东方财富": "300059",
    "汇川技术": "300124",
    "宁德时代": "300750",
    "恒瑞医药": "600276",
    "中天科技": "600522",
    "工业富联": "601138",
    "上海电气": "601727",
    # 常用扩展
    "贵州茅台": "600519",
    "比亚迪": "002594",
    "招商银行": "600036",
    "中国平安": "601318",
    "中信证券": "600030",
    "迈瑞医疗": "300760",
    "药明康德": "603259",
    "隆基绿能": "601012",
    "紫金矿业": "601899",
    "海尔智家": "600690",
    "美的集团": "000333",
    "格力电器": "000651",
    "伊利股份": "600887",
    "海康威视": "002415",
    "三一重工": "600031",
    "通威股份": "600438",
    "长江电力": "600900",
    "中国中免": "601888",
    "药明生物": "02269",  # 港股
    "腾讯控股": "00700",
    "阿里巴巴": "09988",
}


def resolve_ticker(name: str) -> Optional[str]:
    """根据股票名称解析 ticker（XXXXXX.SZ/SS/BJ 格式）"""
    code = NAME_TO_CODE.get(name)
    if not code:
        return None
    if code.startswith("6"):
        return f"{code}.SS"
    elif code.startswith(("8", "4")):
        return f"{code}.BJ"
    elif code.startswith(("0", "3")):
        return f"{code}.SZ"
    else:
        # 港股或其他
        return f"{code}.HK"


def parse_image_to_text(image_path: str) -> str:
    """图片路径 → 提示用户使用 --text 模式 (OCR 由 Agent vision 处理)"""
    print(
        f"[提示] 检测到图片输入: {image_path}", file=sys.stderr)
    print(
        "[提示] 本脚本不使用本地 OCR。请先用大模型 vision 解析截图文本，"
        "保存为 .txt 文件，再通过 --text 传入。", file=sys.stderr)
    sys.exit(2)


def parse_holdings_text(text: str) -> List[Dict]:
    """
    从东方财富持仓截图文本中解析持仓列表.

    支持两种文本格式:
      A) 视觉解析后的结构化文本 (Vision AI 输出)
      B) OCR 原始文本 (PaddleOCR 输出)
    """
    holdings: List[Dict] = []
    lines = text.strip().splitlines()

    # 先尝试格式 A: 每只股票占 2 行 (名称行 + 数据行)
    # 格式 A 示例:
    #   1. TCL科技
    #      市值: 20148.00  持仓/可用: 4600/4600  现价/成本: 4.380/4.371  个股仓位: 3.10%
    # 或 Vision 解析后的表格格式:
    #   TCL科技 | 20148.00 | 4600 | 4600 | 4.380 | 4.371 | 3.10%

    # 策略: 扫描所有行，寻找股票名称，然后在后续行中提取数字
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 策略: 尝试在当前行提取一个短语作为股票名称候选
        # 名称通常在行首，不含数字、冒号、斜杠等
        # 尝试匹配 NAME_TO_CODE 中的每个名称
        stock_name = None
        for known_name in sorted(NAME_TO_CODE.keys(), key=len, reverse=True):
            if known_name in line:
                # 确认这个名称在行首部或单独成字段
                if line.startswith(known_name) or re.search(rf"(?:^|\s|\.|，){re.escape(known_name)}(?:\s|$|\.|，)", line):
                    stock_name = known_name
                    break

        # 如果没有匹配到已知名称，尝试提取临时名称
        if not stock_name:
            # 尝试提取行首的中文/英文组合名称 (排除纯数字和标点)
            m = re.match(r"([A-Za-z]*[\u4e00-\u9fa5][A-Za-z\u4e00-\u9fa5]{1,7})", line)
            if m:
                candidate = m.group(1)
                # 只有当候选名称至少含有两个中文字符时才采纳
                if len(re.findall(r"[\u4e00-\u9fa5]", candidate)) >= 2:
                    stock_name = candidate

        if stock_name:
            ticker = resolve_ticker(stock_name)
            if not ticker:
                i += 1
                continue

            # 在后续 1-5 行中找数据 (东方财富格式每只占5行: 市值/持仓/现价/仓位/盈亏)
            data_lines = []
            for j in range(i + 1, min(i + 6, len(lines))):
                data_lines.append(lines[j])

            data_text = " ".join(data_lines)
            holding = _extract_numbers(data_text, stock_name, ticker)
            if holding:
                holdings.append(holding)
                i += len(data_lines) + 1
                continue
        i += 1

    # 如果没解析到，尝试格式 B: 纯数字表格行
    if not holdings:
        holdings = _parse_table_format(lines)

    return holdings


def _extract_numbers(text: str, stock_name: str, ticker: str) -> Optional[Dict]:
    """
    从数据文本中提取持仓/成本/仓位/市值等数字.
    使用精确正则匹配东方财富字段标签，不依赖数字大小推断.
    """
    t = text.replace(",", "").replace("，", "")

    # 1) 持仓数量 — 匹配 "持仓/可用: 4600 / 4600" 或 "持仓: 4600"
    shares = 0
    m = re.search(r"持仓\s*/\s*可用\s*[:\uff1a]\s*(\d+)\s*/", t)
    if m:
        shares = int(m.group(1))
    else:
        m = re.search(r"持仓\s*[:\uff1a]\s*(\d+)", t)
        if m:
            shares = int(m.group(1))

    # 2) 现价与成本 — 匹配 "现价/成本: 4.380 / 4.371" 或 "成本: 4.371"
    current_price = 0.0
    cost_price = 0.0
    m = re.search(r"现价\s*/\s*成本\s*[:\uff1a]\s*([-+]?[\d.]+)\s*/\s*([-+]?[\d.]+)", t)
    if m:
        current_price = float(m.group(1))
        cost_price = float(m.group(2))
    else:
        # fallback: 单独找成本
        m = re.search(r"成本\s*[:\uff1a]\s*([-+]?[\d.]+)", t)
        if m:
            cost_price = float(m.group(1))
        m = re.search(r"现价\s*[:\uff1a]\s*([-+]?[\d.]+)", t)
        if m:
            current_price = float(m.group(1))

    # 3) 个股仓位
    weight = 0.0
    m = re.search(r"仓位\s*[:\uff1a]\s*(\d+\.?\d*)\s*%", t)
    if m:
        weight = float(m.group(1)) / 100

    # 4) 市值
    market_value = 0.0
    m = re.search(r"市值\s*[:\uff1a]\s*([\d.]+)", t)
    if m:
        market_value = float(m.group(1))

    # 如果至少有股票名称和持仓/成本其中一项，就认为解析成功
    if shares == 0 and cost_price == 0 and market_value == 0:
        return None

    return {
        "name": stock_name,
        "ticker": ticker,
        "shares": shares,
        "cost_price": cost_price,
        "raw_cost_price": cost_price,
        "weight": weight,
        "market_value": market_value,
        "current_price": current_price,
    }


def _parse_table_format(lines: List[str]) -> List[Dict]:
    """备选解析: 表格格式，每行一只股票的完整数据"""
    holdings = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("|"):
            continue

        # 尝试匹配: 名称 + 多个数字
        parts = re.split(r"[\s|]+", line)
        if len(parts) < 4:
            continue

        # 第一个部分可能是名称或序号+名称
        name_part = parts[0]
        # 去掉序号前缀
        name = re.sub(r"^\d+[.、]\s*", "", name_part)

        ticker = resolve_ticker(name)
        if not ticker:
            continue

        # 提取所有数字
        numbers = []
        for p in parts[1:]:
            p = p.replace(",", "").replace("%", "")
            try:
                numbers.append(float(p))
            except ValueError:
                continue

        if len(numbers) < 3:
            continue

        # 启发式分配
        shares = 0
        for n in numbers:
            if n > 100 and n == int(n) and int(n) % 100 == 0:
                shares = int(n)
                break

        prices = [n for n in numbers if 0 < n < 5000 and n != int(n)]
        cost = prices[0] if prices else 0.0

        pcts = [n for n in numbers if 0 < n <= 100 and n not in prices and n != shares]
        weight = pcts[0] / 100 if pcts else 0.0

        holdings.append({
            "name": name,
            "ticker": ticker,
            "shares": shares,
            "cost_price": cost,
            "raw_cost_price": cost,
            "weight": weight,
            "market_value": 0.0,
            "current_price": cost,
        })

    return holdings


def merge_with_existing(
    new_holdings: List[Dict],
    existing_path: Path,
) -> Dict:
    """合并新解析的持仓与现有持仓，以新为准 (覆盖)"""
    output = {
        "updated_at": datetime.now().isoformat(),
        "source": "screenshot_parser",
        "account": "东方财富-信用交易",
        "holdings": new_holdings,
    }

    if existing_path.exists():
        try:
            old = json.loads(existing_path.read_text(encoding="utf-8"))
            output["previous_update"] = old.get("updated_at")
            # 如果新解析缺失某些字段，可回退到旧的 (但这里选择完全覆盖)
        except Exception:
            pass

    return output


def main():
    parser = argparse.ArgumentParser(
        description="持仓文本解析器 → 标准 JSON (OCR 由 Agent vision 处理)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
推荐流程:
  1. Agent 用 vision 大模型解析截图
  2. 将解析结果保存为 .txt 文件
  3. 调用本脚本: %(prog)s --text parsed.txt

示例:
  %(prog)s --text /tmp/holding_parsed.txt
  %(prog)s --text /tmp/holding_parsed.txt --output ~/holdings.json
  %(prog)s --text /tmp/holding_parsed.txt --account "平安证券"
        """,
    )
    parser.add_argument("--image", help="截图图片路径 (不推荐，会提示转用 --text)")
    parser.add_argument("--text", help="大模型 vision 解析后的文本文件路径")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_HOLDINGS_PATH),
        help=f"输出 JSON 路径 (默认: {DEFAULT_HOLDINGS_PATH})",
    )
    parser.add_argument("--account", default="东方财富-信用交易", help="账户名称")
    args = parser.parse_args()

    if not args.image and not args.text:
        parser.print_help()
        sys.exit(1)

    # ── 获取原始文本 ──────────────────────────────
    if args.image:
        print(f"[1/3] OCR 识别图片: {args.image}")
        raw_text = parse_image_to_text(args.image)
        print(f"      识别到 {len(raw_text)} 字符")
    else:
        print(f"[1/3] 读取文本文件: {args.text}")
        raw_text = Path(args.text).read_text(encoding="utf-8")
        print(f"      读取到 {len(raw_text)} 字符")

    # ── 解析持仓 ──────────────────────────────────
    print("[2/3] 解析持仓数据...")
    holdings = parse_holdings_text(raw_text)
    if not holdings:
        print("错误: 未能从文本中解析出任何持仓信息", file=sys.stderr)
        print("--- 原始文本 ---", file=sys.stderr)
        print(raw_text[:1000], file=sys.stderr)
        sys.exit(1)

    print(f"      成功解析 {len(holdings)} 只股票")
    for h in holdings:
        cost_display = f"{h['raw_cost_price']:.3f}"
        print(f"      • {h['name']} ({h['ticker']}): {h['shares']}股, 成本{cost_display}, 占比{h['weight']*100:.2f}%")

    # ── 保存 JSON ─────────────────────────────────
    print("[3/3] 保存持仓 JSON...")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = merge_with_existing(holdings, output_path)
    data["account"] = args.account

    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"      已保存: {output_path}")
    print(f"      共 {len(holdings)} 只持仓")

    # 返回码 0 表示成功，方便脚本调用方判断
    return 0


if __name__ == "__main__":
    sys.exit(main())
