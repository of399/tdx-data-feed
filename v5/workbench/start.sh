#!/bin/bash
# v5-VE1-M2 Workbench 启动脚本
cd "$(dirname "$0")"
exec /home/jiuben/tdx-data-feed/venv/bin/python server.py
