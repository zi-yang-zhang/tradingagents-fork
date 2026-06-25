#!/bin/bash
# 每日市场扫描 + 添加好股到AI智选分组
# 纯执行脚本，不内置时段判断，通过参数控制行为
#
# 用法:
#   bash scripts/cron_daily_scan.sh [strategy] [group] [session_id]
#   参数:
#     strategy: conservative / balanced / aggressive (默认: balanced)
#     group: 目标分组名称 (默认: AI智选)
#     session_id: 时段标识，用于日志命名 (默认: 自动生成时间戳)
#
# 示例:
#   bash scripts/cron_daily_scan.sh balanced "AI智选" morning
#   bash scripts/cron_daily_scan.sh balanced "AI智选" afternoon

set -e

# 关键修复：cron 环境 PATH 极窄
export PATH="/usr/bin:/bin:/usr/local/bin:$PATH"

cd /home/ubuntu/tradingagents-fork
# .venv not present in fork — rely on system python

# 加载环境变量（MX_APIKEY等），但不要让 .bashrc 里的 PATH 覆盖我们的设置
export $(grep -v '^#' ~/.bashrc | grep 'export ' | grep -v 'export PATH' | sed 's/export //g' | xargs 2>/dev/null || true)

# 显式 source ~/.bashrc 以确保 MX_APIKEY 等变量被加载（非交互式 shell 不会自动加载）
if [ -f ~/.bashrc ]; then
    source ~/.bashrc 2>/dev/null || true
fi

# 确保 PATH 始终包含关键目录（防止 .bashrc 或其他来源覆盖）
export PATH="/home/ubuntu/tradingagents-fork/.venv/bin:/usr/bin:/bin:/usr/local/bin:$PATH"

# 确保妙想(MX)优先，避免回退到 westock-data (npx 在 cron 环境可能缺失)
export STOCK_DATA_HUB_MX_FIRST="1"

# 解析参数
STRATEGY="${1:-balanced}"
GROUP="${2:-AI智选}"
SESSION_ID="${3:-}"

PYTHON="/home/ubuntu/tradingagents-fork/.venv/bin/python"
DATE_STR=$(/usr/bin/date '+%Y%m%d_%H%M%S')

# 如果有 session_id，加入日志文件名
if [ -n "$SESSION_ID" ]; then
    LOG_FILE="logs/cron_scan_${SESSION_ID}_${DATE_STR}.log"
else
    LOG_FILE="logs/cron_scan_${DATE_STR}.log"
fi

# 确保日志目录存在
mkdir -p logs

echo "📊 每日市场扫描 | 档位: $STRATEGY | 分组: $GROUP | 时间: $(/usr/bin/date '+%Y-%m-%d %H:%M:%S')"
if [ -n "$SESSION_ID" ]; then
    echo "   会话标识: $SESSION_ID"
fi
echo "   详细日志: $LOG_FILE"
echo ""

# 运行扫描，详细输出重定向到日志，标准输出保留关键结果
"$PYTHON" scripts/daily_scan_and_add.py --strategy "$STRATEGY" --group "$GROUP" > "$LOG_FILE" 2>&1

# 从日志中提取关键结果输出到标准输出（供 notify 使用）
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 扫描结果摘要 | 档位: $STRATEGY | $(/usr/bin/date '+%H:%M:%S')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 提取发现的股票
if /usr/bin/grep -q "扫描发现" "$LOG_FILE"; then
    /usr/bin/grep -A 20 "扫描发现" "$LOG_FILE" | head -25
else
    /usr/bin/grep "发现.*只" "$LOG_FILE" | tail -1 || true
fi

echo ""

# 提取添加结果
if /usr/bin/grep -q "分组更新结果" "$LOG_FILE"; then
    /usr/bin/grep -A 10 "分组更新结果" "$LOG_FILE" | head -10
fi

echo ""

# 提取当前分组总数
if /usr/bin/grep -q "当前.*分组共.*只" "$LOG_FILE"; then
    /usr/bin/grep "当前.*分组共.*只" "$LOG_FILE" | tail -1
fi

echo ""
echo "📁 完整日志: $LOG_FILE"
echo "✅ $(/usr/bin/date '+%Y-%m-%d %H:%M:%S') | 扫描完成"

# ====================================================================
# 🤖 AceTrading 模拟交易执行（与现有扫描共用结果）
# ====================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 AceTrading 模拟交易执行 | 档位: $STRATEGY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

SCAN_ADAPTER="$PYTHON /home/ubuntu/AceTrading/scripts/scan_adapter.py"

# 自动查找最新的候选文件（scan_adapter 内部也会查找，但显式传递更可靠）
CANDIDATES_FILE=$(ls -t /home/ubuntu/tradingagents-fork/stock_analysis_result/scan_candidates_*.json 2>/dev/null | grep -v "latest" | head -1)

if [ -n "$CANDIDATES_FILE" ]; then
    $SCAN_ADAPTER --candidates "$CANDIDATES_FILE" --strategy "$STRATEGY" >> "$LOG_FILE" 2>&1
    
    # 提取 AceTrading 执行摘要到标准输出
    echo ""
    if /usr/bin/grep -q "AceTrading 扫描适配执行报告" "$LOG_FILE"; then
        /usr/bin/grep -A 30 "AceTrading 扫描适配执行报告" "$LOG_FILE" | head -35
    fi
else
    echo "⚠️ 未找到候选文件，跳过 AceTrading 执行"
fi

echo ""
echo "✅ $(/usr/bin/date '+%Y-%m-%d %H:%M:%S') | AceTrading 执行完成"

# ====================================================================
# 📤 推送通知摘要（无论有无入选标的，都输出完整通知到 stdout）
# ====================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📤 扫描通知摘要"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

NOTIFY_JSON="/home/ubuntu/tradingagents-fork/stock_analysis_result/notification_latest.json"
if [ -f "$NOTIFY_JSON" ]; then
    "$PYTHON" -c "
import json, sys
try:
    with open('$NOTIFY_JSON', 'r', encoding='utf-8') as f:
        data = json.load(f)
    print(data.get('message', '📭 通知内容为空'))
except Exception as e:
    print(f'⚠️ 读取通知失败: {e}')
"
else
    echo "⚠️ 未找到通知文件: $NOTIFY_JSON"
fi
