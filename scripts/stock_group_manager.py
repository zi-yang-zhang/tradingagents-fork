#!/usr/bin/env python3
"""
本地自选股分组管理工具
===================
功能：
  - 本地维护分组映射表（弥补 mx-zixuan 查询接口不支持分组的限制）
  - 所有增删操作自动同步到妙想云端自选
  - 支持批量操作、分组导入导出

用法：
  python stock_group_manager.py create AI智选 "AI算力核心标的"
  python stock_group_manager.py add AI智选 601138.SS 000858.SZ
  python stock_group_manager.py remove AI智选 000858.SZ
  python stock_group_manager.py list
  python stock_group_manager.py show AI智选
  python stock_group_manager.py delete AI智选
  python stock_group_manager.py sync
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# ── 配置 ──────────────────────────────────────────
GROUPS_FILE = Path.home() / ".hermes" / "stock_groups.json"

# mx-zixuan skill 路径
MX_ZIXUAN_DIR = Path.home() / "mx-zixuan"
if str(MX_ZIXUAN_DIR) not in sys.path:
    sys.path.insert(0, str(MX_ZIXUAN_DIR))

# 懒加载 mx_zixuan 模块
_mx_module = None


def _get_mx_module():
    global _mx_module
    if _mx_module is None:
        import mx_zixuan as m
        _mx_module = m
    return _mx_module


# ── 分组文件操作 ──────────────────────────────────
def _load_groups() -> Dict:
    """加载本地分组映射表"""
    if not GROUPS_FILE.exists():
        return {"_meta": {"version": "1.0", "updated": datetime.now().isoformat()}, "groups": {}}
    try:
        with open(GROUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"_meta": {"version": "1.0", "updated": datetime.now().isoformat()}, "groups": {}}


def _save_groups(data: Dict):
    """保存本地分组映射表"""
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["_meta"]["updated"] = datetime.now().isoformat()
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_ticker(ticker: str) -> str:
    """标准化ticker格式"""
    ticker = ticker.strip()
    # 纯数字 -> 自动加交易所后缀
    if ticker.isdigit() and len(ticker) == 6:
        if ticker.startswith(("60", "68", "51", "52", "53", "56")):
            return f"{ticker}.SS"
        elif ticker.startswith(("00", "30", "15", "16", "12")):
            return f"{ticker}.SZ"
        elif ticker.startswith(("8", "4", "43")):
            return f"{ticker}.BJ"
    return ticker


def _ticker_to_name(ticker: str) -> str:
    """提取ticker中的6位代码"""
    return ticker.replace(".SS", "").replace(".SZ", "").replace(".BJ", "").replace(".", "")


# ── 云端同步 ──────────────────────────────────────
def _mx_add(ticker: str, group_name: str) -> bool:
    """通过mx-zixuan添加股票到分组"""
    try:
        m = _get_mx_module()
        apikey = m.get_apikey()
        code = _ticker_to_name(ticker)
        # 尝试使用"添加到XXX分组"指令
        queries = [
            f"将{ticker}添加到{group_name}分组",
            f"将{code}添加到{group_name}分组",
        ]
        for q in queries:
            result = m.manage_self_select(apikey, q)
            if result.get("status") == 0 or result.get("message") == "OK":
                return True
        return False
    except Exception as e:
        print(f"  ⚠️  云端添加失败: {e}", file=sys.stderr)
        return False


def _mx_remove(ticker: str, group_name: str) -> bool:
    """通过mx-zixuan从分组删除股票"""
    try:
        m = _get_mx_module()
        apikey = m.get_apikey()
        code = _ticker_to_name(ticker)
        queries = [
            f"从{group_name}分组删除{code}",
            f"从{group_name}分组删除{ticker}",
        ]
        for q in queries:
            result = m.manage_self_select(apikey, q)
            if result.get("status") == 0 or result.get("message") == "OK":
                return True
        return False
    except Exception as e:
        print(f"  ⚠️  云端删除失败: {e}", file=sys.stderr)
        return False


def _mx_sync_to_cloud(group_name: str, tickers: List[str]) -> Dict:
    """将本地分组合完整同步到云端：确保所有股票都在分组中"""
    results = {"added": [], "failed": [], "skipped": []}
    for ticker in tickers:
        if _mx_add(ticker, group_name):
            results["added"].append(ticker)
        else:
            # 可能已经在分组中了
            results["skipped"].append(ticker)
    return results


# ── CLI 命令 ──────────────────────────────────────
def cmd_create(group_name: str, desc: str = ""):
    """创建分组"""
    data = _load_groups()
    if group_name in data["groups"]:
        print(f"❌ 分组 '{group_name}' 已存在")
        return False
    data["groups"][group_name] = {
        "stocks": [],
        "desc": desc or f"{group_name}分组",
        "created": datetime.now().isoformat(),
    }
    _save_groups(data)
    print(f"✅ 已创建分组 '{group_name}'")
    if desc:
        print(f"   描述: {desc}")
    return True


def cmd_delete(group_name: str, force: bool = False):
    """删除分组"""
    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"❌ 分组 '{group_name}' 不存在")
        return False

    tickers = data["groups"][group_name].get("stocks", [])
    if tickers and not force:
        print(f"⚠️  分组 '{group_name}' 包含 {len(tickers)} 只股票")
        print("   使用 --force 强制删除，或先移除股票")
        return False

    # 删除云端分组中的股票
    for ticker in tickers:
        _mx_remove(ticker, group_name)
        print(f"   已从云端移除 {ticker}")

    del data["groups"][group_name]
    _save_groups(data)
    print(f"✅ 已删除分组 '{group_name}'")
    return True


def cmd_add(group_name: str, *tickers: str):
    """添加股票到分组"""
    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"❌ 分组 '{group_name}' 不存在，先创建")
        return False

    normalized = [_normalize_ticker(t) for t in tickers]
    existing = set(data["groups"][group_name].get("stocks", []))

    added = []
    failed = []
    for ticker in normalized:
        if ticker in existing:
            print(f"   ⏭️  {ticker} 已在分组中")
            continue

        print(f"   📤 同步 {ticker} 到云端...")
        if _mx_add(ticker, group_name):
            existing.add(ticker)
            added.append(ticker)
            print(f"   ✅ {ticker} 添加成功")
        else:
            failed.append(ticker)
            print(f"   ❌ {ticker} 云端同步失败")

    data["groups"][group_name]["stocks"] = sorted(list(existing))
    _save_groups(data)

    print(f"\n📊 结果: 成功 {len(added)}, 失败 {len(failed)}, 跳过 {len(tickers) - len(added) - len(failed)}")
    return len(failed) == 0


def cmd_remove(group_name: str, *tickers: str):
    """从分组移除股票"""
    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"❌ 分组 '{group_name}' 不存在")
        return False

    normalized = [_normalize_ticker(t) for t in tickers]
    existing = set(data["groups"][group_name].get("stocks", []))

    removed = []
    failed = []
    for ticker in normalized:
        if ticker not in existing:
            print(f"   ⏭️  {ticker} 不在分组中")
            continue

        print(f"   📤 从云端移除 {ticker}...")
        if _mx_remove(ticker, group_name):
            existing.discard(ticker)
            removed.append(ticker)
            print(f"   ✅ {ticker} 移除成功")
        else:
            failed.append(ticker)
            print(f"   ❌ {ticker} 云端移除失败")

    data["groups"][group_name]["stocks"] = sorted(list(existing))
    _save_groups(data)

    print(f"\n📊 结果: 成功 {len(removed)}, 失败 {len(failed)}, 跳过 {len(tickers) - len(removed) - len(failed)}")
    return len(failed) == 0


def cmd_show(group_name: str):
    """显示分组详情"""
    data = _load_groups()
    if group_name not in data["groups"]:
        print(f"❌ 分组 '{group_name}' 不存在")
        return False

    group = data["groups"][group_name]
    stocks = group.get("stocks", [])
    print(f"\n📂 分组: {group_name}")
    print(f"   描述: {group.get('desc', 'N/A')}")
    print(f"   创建: {group.get('created', 'N/A')}")
    print(f"   股票数: {len(stocks)}")
    if stocks:
        print("\n   股票列表:")
        for i, t in enumerate(stocks, 1):
            print(f"      {i}. {t}")
    print()
    return True


def cmd_list():
    """列出所有分组"""
    data = _load_groups()
    groups = data.get("groups", {})
    if not groups:
        print("📭 没有分组")
        return True

    print(f"\n📂 共有 {len(groups)} 个分组:\n")
    for name, info in groups.items():
        stocks = info.get("stocks", [])
        desc = info.get("desc", "")
        print(f"   📁 {name} ({len(stocks)}只)")
        if desc:
            print(f"      {desc}")
    print()
    return True


def cmd_sync():
    """全量同步：将本地所有分组的股票重新推送到云端"""
    data = _load_groups()
    groups = data.get("groups", {})

    if not groups:
        print("📭 没有分组需要同步")
        return True

    print(f"\n🔄 开始同步 {len(groups)} 个分组到云端...\n")
    for name, info in groups.items():
        stocks = info.get("stocks", [])
        if not stocks:
            continue
        print(f"📁 {name}: {len(stocks)} 只股票")
        results = _mx_sync_to_cloud(name, stocks)
        print(f"   ✅ 成功 {len(results['added'])}  跳过 {len(results['skipped'])}  失败 {len(results['failed'])}")

    print("\n✅ 同步完成")
    return True


def cmd_rename(old_name: str, new_name: str):
    """重命名分组"""
    data = _load_groups()
    if old_name not in data["groups"]:
        print(f"❌ 分组 '{old_name}' 不存在")
        return False
    if new_name in data["groups"]:
        print(f"❌ 分组 '{new_name}' 已存在")
        return False

    data["groups"][new_name] = data["groups"].pop(old_name)
    data["groups"][new_name]["desc"] += f" (原名: {old_name})"
    _save_groups(data)
    print(f"✅ 已将 '{old_name}' 重命名为 '{new_name}'")
    return True


# ── 主入口 ────────────────────────────────────────
USAGE = """
用法: python stock_group_manager.py <命令> [参数...]

命令:
  create  <分组名> [描述]     创建分组
  delete  <分组名> [--force]  删除分组
  rename  <旧名> <新名>       重命名分组
  add     <分组名> <股票...>  添加股票到分组
  remove  <分组名> <股票...>  从分组移除股票
  show    <分组名>            显示分组详情
  list                      列出所有分组
  sync                      全量同步到云端

示例:
  python stock_group_manager.py create AI智选 "AI算力核心标的"
  python stock_group_manager.py add AI智选 601138 000858
  python stock_group_manager.py remove AI智选 000858
  python stock_group_manager.py show AI智选
  python stock_group_manager.py list
  python stock_group_manager.py sync
"""


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(0)

    cmd = args[0]

    try:
        if cmd == "create":
            if len(args) < 2:
                print("用法: create <分组名> [描述]")
                sys.exit(1)
            cmd_create(args[1], " ".join(args[2:]) if len(args) > 2 else "")

        elif cmd == "delete":
            if len(args) < 2:
                print("用法: delete <分组名> [--force]")
                sys.exit(1)
            force = "--force" in args
            cmd_delete(args[1], force=force)

        elif cmd == "rename":
            if len(args) < 3:
                print("用法: rename <旧名> <新名>")
                sys.exit(1)
            cmd_rename(args[1], args[2])

        elif cmd == "add":
            if len(args) < 3:
                print("用法: add <分组名> <股票代码...>")
                sys.exit(1)
            cmd_add(args[1], *args[2:])

        elif cmd == "remove":
            if len(args) < 3:
                print("用法: remove <分组名> <股票代码...>")
                sys.exit(1)
            cmd_remove(args[1], *args[2:])

        elif cmd == "show":
            if len(args) < 2:
                print("用法: show <分组名>")
                sys.exit(1)
            cmd_show(args[1])

        elif cmd == "list":
            cmd_list()

        elif cmd == "sync":
            cmd_sync()

        else:
            print(f"❌ 未知命令: {cmd}")
            print(USAGE)
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n⚠️ 操作已取消")
        sys.exit(1)


if __name__ == "__main__":
    main()
