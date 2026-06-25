#!/bin/bash
# =============================================================================
# 扫描状态检测脚本 - 避免重复执行扫描
# =============================================================================
# 设计原则:
#   1. 优先检查今天的日志文件（最可靠，不受 cron 状态刷新延迟影响）
#   2. 其次检查 notification_latest.json 的时间戳
#   3. 最后检查 cron 输出目录（作为兜底）
#
# 用法:
#   bash scripts/check_scan_status.sh [strategy]
#   返回: 0 = 今天已扫描, 1 = 今天未扫描, 2 = 有日志但可能未完成
#
#   # 在 Python/AI 助手中使用:
#   result=$(bash ~/TradingAgents/scripts/check_scan_status.sh)
#   if [ $? -eq 0 ]; then
#       echo "今天已扫描，直接读取结果"
#   else
#       echo "今天未扫描，可以启动新扫描"
#   fi
# =============================================================================

set -e

STRATEGY="${1:-balanced}"
TODAY=$(date '+%Y%m%d')
TODAY_ISO=$(date '+%Y-%m-%d')

LOGS_DIR="/home/ubuntu/tradingagents-fork/logs"
RESULTS_DIR="/home/ubuntu/tradingagents-fork/stock_analysis_result"
CRON_OUTPUT_DIR="/home/ubuntu/.hermes/cron/output/cbdf4d9ed07e"

# =============================================================================
# 检测函数
# =============================================================================

check_today_logs() {
    local log_pattern="${LOGS_DIR}/cron_scan_${TODAY}*.log"
    local logs
    logs=$(ls -t ${log_pattern} 2>/dev/null || true)

    if [ -n "$logs" ]; then
        local latest_log
        latest_log=$(echo "$logs" | head -1)
        local log_time
        log_time=$(stat -c '%Y' "$latest_log" 2>/dev/null || stat -f '%m' "$latest_log" 2>/dev/null)
        local log_size
        log_size=$(stat -c '%s' "$latest_log" 2>/dev/null || stat -f '%z' "$latest_log" 2>/dev/null)

        # 检查日志是否有效（非空且包含完成标记）
        if [ "$log_size" -gt 100 ]; then
            local has_complete=false
            if grep -qE "扫描完成|通知摘要|执行完成|📤 通知摘要已保存" "$latest_log" 2>/dev/null; then
                has_complete=true
            fi

            if [ "$has_complete" = true ]; then
                echo "✅ 今天已扫描"
                echo "   日志文件: $(basename "$latest_log")"
                echo "   文件大小: ${log_size} bytes"
                echo "   修改时间: $(date -d "@${log_time}" '+%H:%M:%S' 2>/dev/null || date -r "$log_time" '+%H:%M:%S' 2>/dev/null)"
                return 0
            else
                echo "⚠️  今天有日志但未完成"
                echo "   日志文件: $(basename "$latest_log")"
                echo "   文件大小: ${log_size} bytes"
                return 2
            fi
        else
            echo "⚠️  今天有日志但内容为空/过小"
            return 2
        fi
    fi
    return 1
}

check_notification_json() {
    local notify_file="${RESULTS_DIR}/notification_latest.json"

    if [ -f "$notify_file" ]; then
        local file_time
        file_time=$(stat -c '%Y' "$notify_file" 2>/dev/null || stat -f '%m' "$notify_file" 2>/dev/null)
        local file_date
        file_date=$(date -d "@${file_time}" '+%Y%m%d' 2>/dev/null || date -r "$file_time" '+%Y%m%d' 2>/dev/null)

        if [ "$file_date" = "$TODAY" ]; then
            # 验证 JSON 内容
            local found_count
            found_count=$(python3 -c "
import json, sys
try:
    with open('${notify_file}', 'r') as f:
        data = json.load(f)
    print(data.get('found', 'N/A'))
except:
    print('ERROR')
" 2>/dev/null || echo "ERROR")

            if [ "$found_count" != "ERROR" ]; then
                echo "✅ 今天已扫描 (notification_latest.json 确认)"
                echo "   发现股票: ${found_count} 只"
                echo "   修改时间: $(date -d "@${file_time}" '+%H:%M:%S' 2>/dev/null || date -r "$file_time" '+%H:%M:%S' 2>/dev/null)"
                return 0
            fi
        fi
    fi
    return 1
}

check_cron_output() {
    if [ -d "$CRON_OUTPUT_DIR" ]; then
        local latest_cron
        latest_cron=$(ls -t "${CRON_OUTPUT_DIR}"/*.md 2>/dev/null | head -1 || true)

        if [ -n "$latest_cron" ]; then
            local cron_time
            cron_time=$(stat -c '%Y' "$latest_cron" 2>/dev/null || stat -f '%m' "$latest_cron" 2>/dev/null)
            local cron_date
            cron_date=$(date -d "@${cron_time}" '+%Y%m%d' 2>/dev/null || date -r "$cron_time" '+%Y%m%d' 2>/dev/null)

            if [ "$cron_date" = "$TODAY" ]; then
                echo "⚠️  cron 输出显示今天有执行 (可能有延迟)"
                echo "   文件: $(basename "$latest_cron")"
                return 0
            fi
        fi
    fi
    return 1
}

# =============================================================================
# 获取最新扫描结果
# =============================================================================

get_latest_result() {
    local notify_file="${RESULTS_DIR}/notification_latest.json"

    if [ -f "$notify_file" ]; then
        python3 -c "
import json, sys
try:
    with open('${notify_file}', 'r', encoding='utf-8') as f:
        data = json.load(f)
    msg = data.get('message', '无通知内容')
    print(msg)
except Exception as e:
    print(f'读取通知失败: {e}')
"
    else
        echo "未找到通知文件"
    fi
}

get_latest_log_path() {
    ls -t ${LOGS_DIR}/cron_scan_${TODAY}*.log 2>/dev/null | head -1 || echo ""
}

# =============================================================================
# 主逻辑
# =============================================================================

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 扫描状态检测 | 日期: ${TODAY} | 策略: ${STRATEGY}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 优先级 1: 检查今天的日志文件（最可靠）
if check_today_logs; then
    echo ""
    echo "📋 最新扫描结果:"
    echo ""
    get_latest_result
    exit 0
fi

# 如果返回 2（有日志但未完成），继续检查 notification
log_status=$?
if [ "$log_status" -eq 2 ]; then
    echo ""
    echo "继续检查 notification 文件..."
fi

# 优先级 2: 检查 notification_latest.json
if check_notification_json; then
    echo ""
    echo "📋 最新扫描结果:"
    echo ""
    get_latest_result
    exit 0
fi

# 优先级 3: 检查 cron 输出（兜底，可能有延迟）
if check_cron_output; then
    echo ""
    echo "⚠️  注意: cron 状态可能有延迟，建议同时检查日志文件"
    echo "   最新日志: $(get_latest_log_path || echo '无')"
    exit 0
fi

# 未找到今天的扫描记录
echo "❌ 今天尚未执行扫描"
echo ""
echo "💡 建议操作:"
echo "   1. 等待 cron 定时任务自动执行"
echo "   2. 或手动执行: bash ~/TradingAgents/scripts/cron_daily_scan.sh ${STRATEGY}"
exit 1
