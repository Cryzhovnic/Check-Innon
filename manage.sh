#!/usr/bin/env bash
# Меню управления Check-Innon. Запуск: innon
#
# Команды без меню:
#   innon status | on N | off N | token | test | logs | update | uninstall
# N - номер тарифа из меню (1, 2, ...)

set -uo pipefail

DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SERVICE="innon-monitor"
RUN_USER="monitor"
ENV_FILE="$DIR/.env"
WATCHES="$DIR/watches.conf"
PY="/usr/bin/python3"
INSTALL_URL="https://raw.githubusercontent.com/Cryzhovnic/Check-Innon/main/install.sh"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------- тарифы
# Формат строки в watches.conf:  URL|ТАРИФ   или   URL|ТАРИФ|off

ENTRIES=()

load_entries() {
    ENTRIES=()
    [ -f "$WATCHES" ] || return 0
    local line
    while IFS= read -r line; do
        ENTRIES+=("$line")
    done < <(grep -vE '^[[:space:]]*(#|$)' "$WATCHES" || true)
}

entry_url()  { printf '%s' "${1%%|*}"; }
entry_plan() { local r="${1#*|}"; printf '%s' "${r%%|*}"; }
entry_on()   { [[ "$1" != *"|off" ]]; }

plan_info() {
    case "$1" in
        LC-B1) printf 'EPYC 9275F, 399 ₽' ;;
        LC-A1) printf 'Ultra 7 265, 299 ₽' ;;
        *) ;;
    esac
}

ensure_watches_file() {
    # создаём файл, если его нет или в нём нет ни одного тарифа
    if [ ! -f "$WATCHES" ] || ! grep -qvE '^[[:space:]]*(#|$)' "$WATCHES"; then
        cat > "$WATCHES" <<'EOF'
# URL|ТАРИФ[|off] - по строке на тариф. Включай и выключай через меню: innon
https://my.innon.org/epyc-9275f|LC-B1
https://my.innon.org/ultra7-265|LC-A1
EOF
    fi
    chown root:"$RUN_USER" "$WATCHES" 2>/dev/null || true
    chmod 640 "$WATCHES"
}

# переключает n-й тариф (нумерация с 1): mode = on | off | toggle
set_entry() {
    local n="$1" mode="${2:-toggle}" i=0 line tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^[[:space:]]*(#|$) ]]; then
            printf '%s\n' "$line" >> "$tmp"
            continue
        fi
        i=$((i + 1))
        if [ "$i" -eq "$n" ]; then
            local base="${line%|off}"
            base="${base%|on}"
            local want="$mode"
            if [ "$mode" = "toggle" ]; then
                if [[ "$line" == *"|off" ]]; then want="on"; else want="off"; fi
            fi
            if [ "$want" = "off" ]; then line="$base|off"; else line="$base"; fi
        fi
        printf '%s\n' "$line" >> "$tmp"
    done < "$WATCHES"
    cat "$tmp" > "$WATCHES"
    rm -f "$tmp"
}

enabled_count() {
    load_entries
    local e n=0
    for e in "${ENTRIES[@]}"; do
        entry_on "$e" && n=$((n + 1))
    done
    echo "$n"
}

print_entries() {
    load_entries
    if [ "${#ENTRIES[@]}" -eq 0 ]; then
        echo "  (список тарифов пуст)"
        return
    fi
    local i=0 e plan mark info
    for e in "${ENTRIES[@]}"; do
        i=$((i + 1))
        plan="$(entry_plan "$e")"
        info="$(plan_info "$plan")"
        if entry_on "$e"; then mark="ВКЛ  "; else mark="выкл "; fi
        printf '  %d) %-6s [%s] %s\n' "$i" "$plan" "$mark" "$info"
    done
}

# ---------------------------------------------------------------- служба
# Служба сама следует за тарифами: есть включённые - работает, нет - остановлена.

service_active()  { systemctl is-active --quiet "$SERVICE"; }
service_enabled() { systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; }

sync_service() {
    local n
    n="$(enabled_count)"
    if [ "$n" -eq 0 ]; then
        systemctl disable --now "$SERVICE" >/dev/null 2>&1
        echo "Все тарифы выключены, мониторинг остановлен."
    elif service_active; then
        echo "Готово. Изменение подхватится в течение ~15 секунд, в Telegram придёт сообщение."
    else
        systemctl enable --now "$SERVICE" >/dev/null 2>&1
        sleep 2
        if service_active; then
            say "Мониторинг запущен."
        else
            warn "Не запустился, смотри логи."
        fi
    fi
}

cmd_set() {  # cmd_set N on|off|toggle
    local n="$1" mode="$2"
    load_entries
    if ! [[ "$n" =~ ^[0-9]+$ ]] || [ "$n" -lt 1 ] || [ "$n" -gt "${#ENTRIES[@]}" ]; then
        warn "Нет тарифа с номером: $n"
        return 1
    fi
    set_entry "$n" "$mode"
    load_entries
    local e="${ENTRIES[$((n - 1))]}"
    if entry_on "$e"; then
        say "$(entry_plan "$e"): слежение ВКЛЮЧЕНО"
    else
        say "$(entry_plan "$e"): слежение выключено"
    fi
    sync_service
}

cmd_status() {
    local act="выключен"
    service_active && act="РАБОТАЕТ"
    echo "Мониторинг: $act"
    echo "Тарифы:"
    print_entries
    echo
    echo "Последние строки лога:"
    journalctl -u "$SERVICE" -n 8 --no-pager 2>/dev/null || true
}

# ---------------------------------------------------------------- Telegram

cmd_token() {
    local token chat cur_chat="" old_token=""
    if [ -f "$ENV_FILE" ]; then
        cur_chat="$(grep -E '^TG_CHAT_ID=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
        old_token="$(grep -E '^TG_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
    fi
    echo "(Enter - оставить как есть, 0 - отмена)"
    read -rp "Новый TG_TOKEN: " token
    [ "$token" = "0" ] && { echo "Отменено."; return; }
    read -rp "Новый TG_CHAT_ID [${cur_chat:-нет}]: " chat
    [ "$chat" = "0" ] && { echo "Отменено."; return; }

    token="${token:-$old_token}"
    chat="${chat:-$cur_chat}"
    if [ -z "$token" ] || [ -z "$chat" ]; then
        warn "Токен и ID не могут быть пустыми."
        return
    fi
    printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$token" "$chat" > "$ENV_FILE"
    chown root:"$RUN_USER" "$ENV_FILE" 2>/dev/null || true
    chmod 640 "$ENV_FILE"
    say "Сохранено."
    if service_active; then
        systemctl restart "$SERVICE" && say "Служба перезапущена с новыми данными."
    fi
}

cmd_test() {
    if [ ! -f "$ENV_FILE" ]; then
        warn "Нет файла $ENV_FILE"
        return
    fi
    ( set -a; . "$ENV_FILE"; set +a; "$PY" -I "$DIR/monitor.py" --test-telegram )
}

# ---------------------------------------------------------------- прочее

cmd_logs() {
    journalctl -u "$SERVICE" -n 40 --no-pager
    local ans
    read -rp "Следить за логом в реальном времени? y - да, Enter или 0 - назад: " ans
    case "$ans" in
        y|Y|д|Д) echo "(Ctrl+C - выйти из лога)"; journalctl -u "$SERVICE" -f -n 0 || true ;;
    esac
}

cmd_update() {
    if [ -f "$DIR/install.sh" ]; then
        bash "$DIR/install.sh"
    else
        curl -fsSL "$INSTALL_URL" | bash
    fi
}

cmd_uninstall() {
    cat <<EOF

Будет удалено ВСЁ:
  - служба $SERVICE (остановка и автозапуск)
  - папка $DIR (скрипты, токены, список тарифов)
  - пользователь $RUN_USER
  - команда innon (и это меню)
Пакеты python3, git и curl останутся.

EOF
    local ans
    read -rp "Введи yes для удаления (Enter или 0 - отмена): " ans
    if [ "$ans" != "yes" ]; then
        echo "Отменено."
        return
    fi
    if [ -f "$DIR/install.sh" ]; then
        exec bash "$DIR/install.sh" --uninstall
    fi
    curl -fsSL "$INSTALL_URL" | exec bash -s -- --uninstall
}

# ---------------------------------------------------------------- меню

pause() { read -rp "Enter - вернуться в меню " _ || true; }

menu() {
    local choice n i e
    # другие пункты после списка тарифов: "функция|подпись|нужна ли пауза"
    local actions=(
        "cmd_status|Статус и последние строки лога|1"
        "cmd_token|Сменить токен бота / ID получателя|1"
        "cmd_test|Отправить тестовое сообщение в Telegram|1"
        "cmd_logs|Показать логи|0"
        "cmd_update|Обновить из GitHub|1"
        "cmd_uninstall|Удалить всё (бот, служба, скрипты)|1"
    )
    trap ':' INT
    while true; do
        load_entries
        n="${#ENTRIES[@]}"
        local act="выключен"
        service_active && act="РАБОТАЕТ"
        printf '\n\033[1mCheck-Innon\033[0m   мониторинг: %s\n' "$act"
        echo "Тарифы (номер - включить/выключить слежение):"
        print_entries
        echo "Другое:"
        i=0
        for e in "${actions[@]}"; do
            i=$((i + 1))
            IFS='|' read -r _ label _ <<< "$e"
            printf '  %d) %s\n' "$((n + i))" "$label"
        done
        echo "  0) Выход"
        read -rp "> " choice || { echo; break; }
        echo
        case "$choice" in
            0|q|Q) break ;;
            "") continue ;;
        esac
        if ! [[ "$choice" =~ ^[0-9]+$ ]]; then
            warn "Нет такого пункта"
            continue
        fi
        if [ "$choice" -ge 1 ] && [ "$choice" -le "$n" ]; then
            cmd_set "$choice" toggle
        elif [ "$choice" -gt "$n" ] && [ "$choice" -le "$((n + ${#actions[@]}))" ]; then
            IFS='|' read -r fn _ wait <<< "${actions[$((choice - n - 1))]}"
            "$fn"
            [ "$wait" = "1" ] && pause
        else
            warn "Нет такого пункта"
        fi
    done
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            exec sudo "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
        fi
        echo "Запусти от root." >&2
        exit 1
    fi
    ensure_watches_file
    case "${1:-}" in
        "")        menu ;;
        status)    cmd_status ;;
        on)        cmd_set "${2:-}" on ;;
        off)       cmd_set "${2:-}" off ;;
        token)     cmd_token ;;
        test)      cmd_test ;;
        logs)      cmd_logs ;;
        update)    cmd_update ;;
        uninstall) cmd_uninstall ;;
        *) echo "Неизвестная команда: $1" >&2; exit 1 ;;
    esac
}

# при `source manage.sh` (для тестов) меню не запускается
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
