#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f tunnel.pid ]; then
    echo "tunnel.pid not found - tunnel is not running"
    exit 1
fi

PID=$(cat tunnel.pid)
# минус перед PID = убить всю группу процессов (цикл + autossh/ssh)
kill -TERM -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null
# подчистить осиротевший ssh/autossh, если остался
pkill -f "0.0.0.0:22022:localhost:22" 2>/dev/null
rm -f tunnel.pid
echo "Tunnel stopped (was PID $PID)"
