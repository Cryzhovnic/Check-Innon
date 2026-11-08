#!/usr/bin/env bash
# Установка / обновление Check-Innon на Debian 12/13 (и близких).
#
#   Установка:   curl -fsSL https://raw.githubusercontent.com/Cryzhovnic/Check-Innon/main/install.sh | bash
#   Обновление:  то же самое (токены не трогаются)
#   Удаление:    ... | bash -s -- --uninstall
#
# Токен и ID можно передать заранее, тогда вопросов не будет:
#   TG_TOKEN=... TG_CHAT_ID=... bash install.sh

set -euo pipefail

REPO="https://github.com/Cryzhovnic/Check-Innon.git"
DIR="/opt/innon-monitor"
SERVICE="innon-monitor"
RUN_USER="monitor"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запусти от root (или через sudo)."
command -v systemctl >/dev/null || die "Нужен systemd."

# ---------- удаление ----------
if [ "${1:-}" = "--uninstall" ]; then
    say "Удаляю службу и файлы"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE}.service"
    systemctl daemon-reload
    rm -rf "$DIR"
    id "$RUN_USER" >/dev/null 2>&1 && userdel "$RUN_USER" || true
    say "Готово"
    exit 0
fi

# ---------- пакеты ----------
say "Ставлю пакеты (python3, git, curl, ca-certificates)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git curl ca-certificates >/dev/null

# ---------- пользователь ----------
if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "Создаю пользователя $RUN_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
fi

# ---------- код ----------
if [ -d "$DIR/.git" ]; then
    say "Обновляю код"
    git -C "$DIR" pull --ff-only -q
else
    say "Скачиваю код в $DIR"
    [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && die "$DIR уже существует и не пустой."
    git clone -q "$REPO" "$DIR"
fi

# ---------- токены ----------
ENV_FILE="$DIR/.env"

ask_tty() {  # ask_tty "вопрос" [silent]
    local prompt="$1" silent="${2:-}" val=""
    [ -r /dev/tty ] || die "Нет терминала для вопросов. Передай TG_TOKEN и TG_CHAT_ID переменными окружения."
    if [ "$silent" = "silent" ]; then
        read -rsp "$prompt" val </dev/tty; echo >&2
    else
        read -rp "$prompt" val </dev/tty
    fi
    printf '%s' "$val"
}

if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
    say "Беру токен и ID из переменных окружения"
    printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$TG_TOKEN" "$TG_CHAT_ID" > "$ENV_FILE"
elif [ -s "$ENV_FILE" ]; then
    say "Найден существующий .env, оставляю как есть"
else
    echo
    echo "Токен бота берётся у @BotFather (/newbot)."
    echo "Свой числовой ID можно узнать у @userinfobot."
    echo "Не забудь написать своему боту /start, иначе он не сможет писать тебе."
    echo
    TOKEN="$(ask_tty 'TG_TOKEN (ввод скрыт): ' silent)"
    CHAT="$(ask_tty 'TG_CHAT_ID: ')"
    [ -n "$TOKEN" ] && [ -n "$CHAT" ] || die "Токен и ID не могут быть пустыми."
    printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$TOKEN" "$CHAT" > "$ENV_FILE"
fi

# токен читает только root и служба
chown root:"$RUN_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# быстрая проверка токена
set -a; . "$ENV_FILE"; set +a
if curl -fsS -m 10 "https://api.telegram.org/bot${TG_TOKEN}/getMe" >/dev/null 2>&1; then
    say "Токен принят Telegram"
else
    warn "Telegram не подтвердил токен (неверный токен или нет доступа к api.telegram.org с этого сервера)."
fi

# ---------- служба ----------
say "Запускаю службу $SERVICE"
systemctl enable "$DIR/${SERVICE}.service" >/dev/null 2>&1 || systemctl link "$DIR/${SERVICE}.service" >/dev/null
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
    say "Работает. В Telegram должно прийти сообщение «Мониторинг LC-B1 запущен»."
else
    warn "Служба не запустилась. Последние строки лога:"
    journalctl -u "$SERVICE" -n 15 --no-pager || true
    exit 1
fi

cat <<EOF

Полезные команды:
  systemctl status $SERVICE        состояние
  journalctl -u $SERVICE -f        живой лог
  systemctl restart $SERVICE       перезапуск
  Обновить:  запусти установщик ещё раз
  Удалить:   curl -fsSL https://raw.githubusercontent.com/Cryzhovnic/Check-Innon/main/install.sh | bash -s -- --uninstall
Файлы: $DIR (токены в $ENV_FILE)
EOF
