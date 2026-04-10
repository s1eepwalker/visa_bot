#!/bin/bash
cd "$(dirname "$0")"

if [ -f bot.pid ]; then
    PID=$(cat bot.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "Bot already running PID $PID"
        exit 0
    fi
    rm bot.pid
fi

nohup python3 monitor.py > monitor.out 2> monitor.err &
echo $! > bot.pid
echo "Bot started PID $(cat bot.pid)"
