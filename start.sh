#!/bin/bash
echo "📈 股票K线AI分析服务启动中..."
if ! pip show fastapi > /dev/null 2>&1; then
    echo "📦 安装Python依赖..."
    pip install -r requirements.txt
fi
mkdir -p static templates
echo "🚀 服务启动: http://0.0.0.0:8000"
python main.py