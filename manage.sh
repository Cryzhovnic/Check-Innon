#!/usr/bin/env bash
# Меню управления Check-Innon. Запуск: innon
#
# Команды без меню:  innon status | start | stop | restart | list | add | remove |
#                    token | test | logs | update | uninstall

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

# ---------------------------------------------------------------- список тарифов

ENTRIES=()

load_entries() {
    ENTRIES=()
    [ -f "$WATCHES" ] || return 0
    local line
    while IFS= read -r line; do
        ENTRIES+=("$line")
    done < <(grep -vE '^[[:space:]]*(#|$)' "$WATCHES" || true)
}

print_entries() {
    load_entries
    if [ "${#ENTRIES[@]}" -eq 0 ]; then
        echo "  (список пуст)"
        return
    fi
    local i=0 e
    for e in "${ENTRIES[@]}"; do
        i=$((i + 1))
        printf '  %d) %s   %s\n' "$i" "${e#*|}" "${e%%|*}"
    done
}

ensure_watches_file() {
    if [ ! -f "$WATCHES" ]; then
        printf '# URL|ТАРИФ - по строке на тариф. Управляй через меню: innon\n' > "$WATCHES"
    fi
    chown root:"$RUN_USER" "$WATCHES" 2>/dev/null || true
    chmod 640 "$WATCHES"
}

# разбор выбора "1 3", "1,3" или "a" в индексы 1..$2; результат в массиве PICKED
parse_selection() {
    local sel="$1" max="$2" tok
    PICKED=()
    sel="${sel//,/ }"
    case "$sel" in
        a|A|all|все) for ((tok = 1; tok <= max; tok++)); do PICKED+=("$tok"); done; return 0 ;;
    esac
    for tok in $sel; do
        if [[ "$tok" =~ ^[0-9]+$ ]] && [ "$tok" -ge 1 ] && [ "$tok" -le "$max" ]; then
            PICKED+=("$tok")
        else
            warn "Пропускаю неверный номер: $tok"
        fi
    done
    [ "${#PICKED[@]}" -gt 0 ]
}

# ---------------------------------------------------------------- служба

service_active()  { systemctl is-active --quiet "$SERVICE"; }
service_enabled() { systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; }

restart_service() {
    if service_active; then
        systemctl restart "$SERVICE" && say "Служба перезапущена, изменения применены."
    else
        echo "Мониторинг сейчас выключен, изменения вступят в силу при включении."
    fi
}

cmd_status() {
    local act="выключен" aut="нет"
    service_active && act="РАБОТАЕТ"
    service_enabled && aut="да"
    echo "Мониторинг: $act (автозапуск: $aut)"
    echo "Отслеживаемые тарифы:"
    print_entries
    echo
    echo "Последние строки лога:"
    journalctl -u "$SERVICE" -n 8 --no-pager 2>/dev/null || true
}

cmd_start() {
    load_entries
    if [ "${#ENTRIES[@]}" -eq 0 ]; then
        warn "Список тарифов пуст. Сначала добавь тариф (пункт 5)."
        return
    fi
    systemctl enable --now "$SERVICE" >/dev/null 2>&1
    sleep 2
    if service_active; then say "Мониторинг включён."; else warn "Не запустился, смотри логи (пункт 9)."; fi
}

cmd_stop() {
    systemctl disable --now "$SERVICE" >/dev/null 2>&1
    say "Мониторинг выключен и убран из автозапуска."
}

cmd_restart() {
    systemctl restart "$SERVICE" && say "Перезапущено."
}

# ---------------------------------------------------------------- тарифы

cmd_list() {
    echo "Отслеживаемые тарифы:"
    print_entries
}

cmd_add() {
    local url out name price st i=0 sel
    read -rp "Ссылка на страницу с тарифами (например https://my.innon.org/ultra7-265): " url
    url="${url//[[:space:]]/}"
    case "$url" in
        http://*|https://*|file://*) ;;
        *) warn "Нужна ссылка, начинающаяся с https://"; return ;;
    esac

    echo "Загружаю страницу..."
    if ! out="$("$PY" -I "$DIR/monitor.py" --list "$url" 2>&1)"; then
        warn "$out"
        return
    fi

    local names=()
    echo "Тарифы на странице:"
    while IFS=$'\t' read -r name price st; do
        [ -n "$name" ] || continue
        i=$((i + 1))
        names+=("$name")
        case "$st" in
            yes) st="В НАЛИЧИИ" ;;
            no)  st="нет в наличии" ;;
            *)   st="статус неясен" ;;
        esac
        printf '  %d) %s  %s  [%s]\n' "$i" "$name" "$price" "$st"
    done <<< "$out"
    [ "$i" -gt 0 ] || { warn "Тарифы не найдены."; return; }

    read -rp "Номера для слежения (через пробел или запятую, a - все): " sel
    parse_selection "$sel" "$i" || { warn "Ничего не выбрано."; return; }

    ensure_watches_file
    local n entry added=0
    for n in "${PICKED[@]}"; do
        entry="$url|${names[$((n - 1))]}"
        if grep -qxF -- "$entry" "$WATCHES"; then
            echo "  ${names[$((n - 1))]} уже отслеживается"
        else
            printf '%s\n' "$entry" >> "$WATCHES"
            echo "  + ${names[$((n - 1))]}"
            added=1
        fi
    done
    [ "$added" -eq 1 ] && restart_service
}

cmd_remove() {
    load_entries
    if [ "${#ENTRIES[@]}" -eq 0 ]; then
        echo "Список пуст."
        return
    fi
    echo "Отслеживаемые тарифы:"
    print_entries
    local sel
    read -rp "Номера для удаления (через пробел или запятую, a - все): " sel
    parse_selection "$sel" "${#ENTRIES[@]}" || { warn "Ничего не выбрано."; return; }

    declare -A drop=()
    local n
    for n in "${PICKED[@]}"; do drop["${ENTRIES[$((n - 1))]}"]=1; done

    local tmp line
    tmp="$(mktemp)"
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^[[:space:]]*(#|$) ]] || [ -z "${drop[$line]:-}" ]; then
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$WATCHES"
    cat "$tmp" > "$WATCHES"
    rm -f "$tmp"
    say "Удалено из списка: ${#drop[@]}"

    load_entries
    if [ "${#ENTRIES[@]}" -eq 0 ]; then
        warn "Список пуст: мониторингу нечего проверять. Добавь тариф (пункт 5) или выключи мониторинг (пункт 3)."
        systemctl stop "$SERVICE" 2>/dev/null || true
    else
        restart_service
    fi
}

# ---------------------------------------------------------------- Telegram

cmd_token() {
    local token chat cur_chat=""
    [ -f "$ENV_FILE" ] && cur_chat="$(grep -E '^TG_CHAT_ID=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
    read -rsp "Новый TG_TOKEN (ввод скрыт, пусто = не менять): " token; echo
    read -rp "Новый TG_CHAT_ID [${cur_chat:-нет}] (пусто = не менять): " chat

    local old_token=""
    [ -f "$ENV_FILE" ] && old_token="$(grep -E '^TG_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
    token="${token:-$old_token}"
    chat="${chat:-$cur_chat}"
    if [ -z "$token" ] || [ -z "$chat" ]; then
        warn "Токен и ID не могут быть пустыми."
        return
    fi
    printf 'TG_TOKEN=%s\nTG_CHAT_ID=%s\n' "$token" "$chat" > "$ENV_FILE"
    chown root:"$RUN_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    say "Сохранено."
    restart_service
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
    read -rp "Следить за логом в реальном времени? (Ctrl+C - выйти из лога) [y/N]: " ans
    case "$ans" in
        y|Y|д|Д) journalctl -u "$SERVICE" -f -n 0 || true ;;
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
    read -rp "Для подтверждения введи yes: " ans
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

header() {
    load_entries
    local act="выключен"
    service_active && act="РАБОТАЕТ"
    printf '\n\033[1mCheck-Innon\033[0m   мониторинг: %s   тарифов: %d\n' "$act" "${#ENTRIES[@]}"
}

pause() { read -rp "Enter - вернуться в меню " _; }

menu() {
    local choice
    trap ':' INT
    while true; do
        header
        cat <<'EOF'
  1) Статус
  2) Включить мониторинг
  3) Выключить мониторинг
  4) Перезапустить
  5) Добавить тариф для слежения
  6) Убрать тариф из слежения
  7) Сменить токен бота / ID получателя
  8) Отправить тестовое сообщение в Telegram
  9) Показать логи
 10) Обновить из GitHub
 11) Удалить всё (бот, служба, скрипты)
  0) Выход
EOF
        read -rp "> " choice || { echo; break; }
        echo
        case "$choice" in
            1) cmd_status ;;
            2) cmd_start ;;
            3) cmd_stop ;;
            4) cmd_restart ;;
            5) cmd_add ;;
            6) cmd_remove ;;
            7) cmd_token ;;
            8) cmd_test ;;
            9) cmd_logs ;;
            10) cmd_update ;;
            11) cmd_uninstall ;;
            0|q|Q) break ;;
            "") continue ;;
            *) warn "Нет такого пункта" ;;
        esac
        [ "$choice" = "0" ] || pause
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
        start)     cmd_start ;;
        stop)      cmd_stop ;;
        restart)   cmd_restart ;;
        list)      cmd_list ;;
        add)       cmd_add ;;
        remove)    cmd_remove ;;
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
