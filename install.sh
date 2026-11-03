#!/usr/bin/env bash
# Установка бота закупок на сервер Ubuntu/Debian одной командой:
#   curl -fsSL https://raw.githubusercontent.com/AniFrileN/J2B/ccr-a061dd1d-xro167/install.sh | sudo bash
# Повторный запуск обновляет бота, настройки (.env) и базу не трогает.
set -euo pipefail

REPO=https://github.com/AniFrileN/J2B.git
BRANCH=ccr-a061dd1d-xro167
DIR=/opt/zakup-bot
SVC=zakup-bot
BOT_USER=zakupbot

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
ask() {  # ask VAR "Вопрос" [secret]
  local __val=""
  while [ -z "$__val" ]; do
    if [ "${3:-}" = secret ]; then
      read -rsp "$2: " __val </dev/tty; echo
    else
      read -rp "$2: " __val </dev/tty
    fi
  done
  printf -v "$1" '%s' "$__val"
}

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo"; exit 1; }

say "Проверяю, в какой стране сервер"
country=$(curl -fsS --max-time 10 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]' || true)
echo "Страна: ${country:-не удалось определить}"
if [ "$country" = "RU" ]; then
  echo "Сервер в России: Gemini и Claude с российских IP не работают."
  read -rp "Всё равно продолжить? [y/N] " a </dev/tty
  [[ "$a" =~ ^[YyДд] ]] || exit 1
fi

say "Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl python3 python3-venv >/dev/null

PY=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  say "$(python3 -V) слишком старый, ставлю Python 3.11"
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq python3.11 python3.11-venv >/dev/null
  PY=python3.11
fi

say "Создаю отдельного пользователя $BOT_USER (бот не получит доступа к сайту)"
id -u "$BOT_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$BOT_USER"

say "Скачиваю бота в $DIR"
if [ -d "$DIR/.git" ]; then
  git -c safe.directory="$DIR" -C "$DIR" fetch -q origin "$BRANCH"
  git -c safe.directory="$DIR" -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

if [ ! -f "$DIR/.env" ]; then
  say "Настройка (ввод ключей не отображается на экране — это нормально)"
  ask BOT_TOKEN "Токен бота от @BotFather" secret
  echo "Какой ИИ использовать: 1 — Gemini (бесплатно), 2 — Claude (платно)"
  read -rp "Выбор [1]: " choice </dev/tty
  if [ "${choice:-1}" = 2 ]; then
    AI=claude; KEYVAR=ANTHROPIC_API_KEY; ask KEY "Ключ Claude" secret
  else
    AI=gemini; KEYVAR=GEMINI_API_KEY; ask KEY "Ключ Gemini" secret
  fi
  read -rp "Ваш Telegram ID (можно пропустить — Enter): " ADMIN </dev/tty
  (
    umask 077
    printf 'BOT_TOKEN=%s\nAI=%s\n%s=%s\nADMIN_IDS=%s\n' \
      "$BOT_TOKEN" "$AI" "$KEYVAR" "$KEY" "${ADMIN:-}" > "$DIR/.env"
  )
else
  echo "Настройки $DIR/.env уже есть — оставляю как есть."
fi

say "Ставлю зависимости Python (пару минут)"
[ -d "$DIR/.venv" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
chown -R "$BOT_USER:$BOT_USER" "$DIR"
chmod 600 "$DIR/.env"

say "Настраиваю автозапуск"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=Telegram-бот закупок
After=network-online.target
Wants=network-online.target

[Service]
User=$BOT_USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m zakup_bot
Restart=always
RestartSec=10
# Не даём боту занять память сайта
MemoryMax=400M
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SVC"
systemctl restart "$SVC"
sleep 8

if systemctl is-active -q "$SVC"; then
  say "Бот запущен! Напишите ему /start в Telegram."
else
  echo
  echo "Бот не запустился. Последние строки журнала:"
  journalctl -u "$SVC" -n 30 --no-pager
  exit 1
fi

cat <<EOF

Полезные команды:
  systemctl status $SVC                 — работает ли бот
  journalctl -u $SVC -f                 — журнал (выход Ctrl+C)
  nano $DIR/.env                        — поменять настройки, затем:
  systemctl restart $SVC                — перезапуск
Обновить бота: запустите команду установки ещё раз.
EOF
