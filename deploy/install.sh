#!/usr/bin/env bash
# Установка монитора на чистый Ubuntu/Debian VPS.
# Запускать от root:
#   bash install.sh
set -euo pipefail

APP_DIR=/opt/sapsan-watch
REPO=https://github.com/pmcases/sapsan-watch.git

echo "==> ставим зависимости"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git curl >/dev/null

echo "==> забираем код"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR"

# ─────────── секреты ───────────
if [ ! -f "$APP_DIR/.env" ]; then
  echo
  echo "Нужны два значения из Telegram."
  read -rp "  TELEGRAM_TOKEN (от @BotFather): " TG_TOKEN
  read -rp "  TELEGRAM_CHAT_ID (от @userinfobot): " TG_CHAT
  cat > "$APP_DIR/.env" <<EOF
TELEGRAM_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT
TRIP_DATE=20.09.2026
TIME_FROM=09:20
TIME_TO=09:50
PRICE_THRESHOLD=14000
DROP_DELTA=1000
PRICE_CEILING=30000
WANTED_CLASSES=
DAILY_DIGEST_HOUR=10
STATE_PATH=$APP_DIR/state.json
EOF
  chmod 600 "$APP_DIR/.env"
  echo "  сохранил в $APP_DIR/.env"
else
  echo "==> .env уже есть, не трогаю"
fi

# ─────────── проверка связи ───────────
echo
echo "==> проверяем, что с этой машины всё видно"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
RZD=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 -A "$UA" "https://pass.rzd.ru/" || echo "000")
TG=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "https://api.telegram.org/" || echo "000")
echo "    РЖД:      $RZD   (нужно 200)"
echo "    Telegram: $TG   (нужно 302 или 200)"
case "$RZD" in 200|30*) ;; *) echo "    !! РЖД не отвечает — проверь, что VPS реально в России" ;; esac
case "$TG"  in 200|30*) ;; *) echo "    !! Telegram не отвечает с этого хостинга" ;; esac

# ─────────── systemd ───────────
echo
echo "==> ставим таймер"
cat > /etc/systemd/system/sapsan-watch.service <<EOF
[Unit]
Description=Монитор билетов на Сапсан
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/python3 $APP_DIR/sapsan_watch.py
StandardOutput=append:/var/log/sapsan-watch.log
StandardError=append:/var/log/sapsan-watch.log
EOF

cat > /etc/systemd/system/sapsan-watch.timer <<EOF
[Unit]
Description=Проверять цены каждые 5 минут

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now sapsan-watch.timer >/dev/null

echo
echo "==> первый прогон"
systemctl start sapsan-watch.service || true
sleep 2
tail -n 40 /var/log/sapsan-watch.log 2>/dev/null || echo "(лог пока пуст)"

cat <<'TXT'

Готово. Дальше полезное:

  журнал         tail -f /var/log/sapsan-watch.log
  прогнать руками systemctl start sapsan-watch
  когда следующий systemctl list-timers sapsan-watch.timer
  поменять порог  nano /opt/sapsan-watch/.env   (потом ничего перезапускать не надо)
  выключить       systemctl disable --now sapsan-watch.timer

TXT
