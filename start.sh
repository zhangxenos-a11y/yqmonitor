#!/bin/bash
# 舆情监测系统 - 一键启动（后台常驻）
cd "$(dirname "$0")"
mkdir -p instance

if [ ! -d .venv ]; then
  echo "首次运行，创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi

# 已在运行则先停
if [ -f instance/server.pid ]; then
  kill "$(cat instance/server.pid)" 2>/dev/null
  rm -f instance/server.pid
fi

nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > instance/server.log 2>&1 &
echo $! > instance/server.pid
sleep 2
echo "✅ 已启动，PID $(cat instance/server.pid)"
echo "   访问：http://localhost:8000  （局域网内其它设备用 http://本机IP:8000）"
echo "   日志：tail -f instance/server.log"
