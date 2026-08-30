#!/bin/bash
# Постоянный ОБРАТНЫЙ туннель VPS -> radmin0.
# VPS инициирует исходящее SSH к radmin0 и пробрасывает свой sshd (:22) на radmin0.
# Доступ к VPS идёт по уже установленному VPS-инициированному каналу, поэтому
# обходит blackhole входящих маршрутов к сети hoster.kg.
#
# Точка встречи = radmin0 (VPS и клиент оба её видят; пара VPS<->radmin0 НЕ под blackhole,
# в отличие от домашнего NAS/hessen). Ключ id_rsa-VSCODE уже авторизован на radmin0.
#
# Доступ с клиента:  ssh -J radmin0 -p 22022 andrey@127.0.0.1   (или Host vps-tun из config)

cd "$(dirname "$0")"

MEET_HOST=93.91.171.46          # radmin0
MEET_PORT=22
MEET_USER=andrey
KEY="$HOME/.ssh/id_rsa-VSCODE"  # уже авторизован на radmin0
BIND="127.0.0.1:22022"          # на radmin0 (GatewayPorts=no → только loopback); доступ через ProxyJump radmin0
TARGET="localhost:22"

OPTS="-N -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=accept-new \
  -o BatchMode=yes \
  -o TCPKeepAlive=yes \
  -i $KEY \
  -p $MEET_PORT \
  -R ${BIND}:${TARGET} \
  ${MEET_USER}@${MEET_HOST}"

if command -v autossh >/dev/null 2>&1; then
    export AUTOSSH_GATETIME=0
    exec autossh -M 0 $OPTS
else
    while true; do
        ssh $OPTS
        echo "$(date '+%F %T') туннель упал, переподключаюсь через 10с" >> tunnel.log
        sleep 10
    done
fi
