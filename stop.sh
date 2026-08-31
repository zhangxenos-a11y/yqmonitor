#!/bin/bash
# 舆情监测系统 - 停止
cd "$(dirname "$0")"
if [ -f instance/server.pid ]; then
  kill "$(cat instance/server.pid)" 2>/dev/null
  rm -f instance/server.pid
  echo "✅ 已停止"
else
  pkill -f "uvicorn app.main:app" 2>/dev/null && echo "✅ 已停止" || echo "⚠️ 未在运行"
fi
