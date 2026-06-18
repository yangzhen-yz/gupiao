#!/bin/bash
# macOS 一键启动
cd "$(dirname "$0")"
python3 start.py 2>/dev/null || python start.py
