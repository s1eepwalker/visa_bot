#!/bin/bash
cd "$(dirname "$0")"

if [ -f tunnel.pid ] && kill -0 "$(cat tunnel.pid)" 2>/dev/null; then
    echo "Tunnel already running PID $(cat tunnel.pid)"
    exit 0
fi
rm -f tunnel.pid

# setsid -> отдельная группа процессов, чтобы stop мог убить и цикл, и дочерний ssh
setsid nohup ./autotunnel.sh >>tunnel.out 2>>tunnel.err &
echo $! > tunnel.pid
echo "Tunnel started PID $(cat tunnel.pid)"
