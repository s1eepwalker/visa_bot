#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f bot.pid ]; then
    echo "bot.pid not found - bot is not running"
    exit 1
fi

PID=$(cat bot.pid)
kill "$PID" 2>/dev/null && echo "Bot stopped PID $PID" || echo "Process $PID not found"
rm -f bot.pid
