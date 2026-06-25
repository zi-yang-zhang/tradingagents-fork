#!/bin/bash
# 周五收市后分组回顾分析 (Cron Wrapper)

set -e
cd /home/ubuntu/tradingagents-fork
source .venv/bin/activate

# 加载环境变量（MX_APIKEY等）
export $(grep -v '^#' ~/.bashrc | grep 'export ' | sed 's/export //g' | xargs 2>/dev/null || true)

# 显式 source ~/.bashrc 以确保 MX_APIKEY 等变量被加载（非交互式 shell 不会自动加载）
if [ -f ~/.bashrc ]; then
    source ~/.bashrc 2>/dev/null || true
fi

echo "📋 $(date '+%Y-%m-%d %H:%M:%S') | 开始周五分组回顾分析"
python scripts/weekly_group_review.py --group "AI智选"
echo "✅ $(date '+%Y-%m-%d %H:%M:%S') | 回顾完成"
