#!/usr/bin/env python3
"""
Check-Innon: следит за тарифами на my.innon.org и шлёт сообщение в Telegram,
когда тариф становится доступен для покупки (одно сообщение, без повторов).

Если написать боту любое сообщение, он ответит статусом: за чем следит,
что сейчас в наличии, как давно проверял и сколько работает.

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.

Список тарифов лежит в файле watches.conf рядом со скриптом, по строке на тариф:
    https://my.innon.org/epyc-9275f|LC-B1
    https://my.innon.org/ultra7-265|LC-A1|off      <- выключенный тариф
Строки с # и пустые игнорируются. Включать и выключать тарифы удобнее через меню: innon
Файл перечитывается на каждом круге, перезапуск после правки не нужен.

Запуск:
    export TG_TOKEN="123456:ABC..."     # токен бота от @BotFather
    export TG_CHAT_ID="123456789"       # твой числовой Telegram ID
    python3 monitor.py                  # слежение
    python3 monitor.py --test-telegram  # отправить тестовое сообщение
    python3 monitor.py --version        # версия
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHES_FILE = os.environ.get("WATCHES_FILE", os.path.join(BASE_DIR, "watches.conf"))
DEFAULT_WATCHES = [("https://my.innon.org/epyc-9275f", "LC-B1")]


def read_version() -> str:
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = read_version()

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

CHECK_EVERY = 10         # секунд между кругами проверки (плюс небольшой джиттер)
FAILS_BEFORE_ALERT = 10  # сколько ошибок подряд по странице до предупреждения
EX_CONFIG = 78           # код выхода "нечего делать", systemd не перезапускает

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}

NAME_RE = re.compile(r"<h3[^>]*>\s*([^<]+?)\s*</h3>")
PRICE_RE = re.compile(r"(\d[\d\s.,]*₽)")


# ---------------------------------------------------------------- Telegram

def tg_api(method: str, params: dict, timeout: float) -> dict:
    api = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_send(text: str) -> bool:
    """Отправляет сообщение, до 3 попыток. Возвращает True при успехе."""
    params = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    for attempt in range(3):
        try:
            tg_api("sendMessage", params, 15)
            return True
        except Exception as e:
            print(f"[!] Telegram, попытка {attempt + 1}/3: {e}", file=sys.stderr)
            time.sleep(2)
    return False


def skip_old_updates() -> int:
    """Пропускает сообщения, написанные боту до запуска. Возвращает offset для getUpdates."""
    try:
        res = tg_api("getUpdates", {"offset": -1, "timeout": 0}, 15).get("result", [])
        return res[-1]["update_id"] + 1 if res else 0
    except Exception as e:
        print(f"[!] getUpdates (старт): {e}", file=sys.stderr)
        return 0


# ---------------------------------------------------------------- страница

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_cards(html: str) -> dict:
    """
    Возвращает {название_тарифа: (доступен, цена)}.
    доступен: True / False / None (не удалось понять).

    Каждый тариф - блок с class="content-box ...". Внутри:
      недоступен -> <a ...>Нет в наличии</a>
      доступен   -> <button wire:click="addToCart">Добавить в корзину</button>
    """
    cards = {}
    for chunk in html.split('class="content-box')[1:]:
        m = NAME_RE.search(chunk)
        if not m:
            continue
        if "Нет в наличии" in chunk:
            available = False
        elif 'wire:click="addToCart"' in chunk or "Добавить в корзину" in chunk:
            available = True
        else:
            available = None
        pm = PRICE_RE.search(chunk)
        price = re.sub(r"\s+", " ", pm.group(1)).strip() if pm else ""
        cards[m.group(1)] = (available, price)
    return cards


# ---------------------------------------------------------------- список слежения

def load_all_watches(path: str = None) -> list:
    """Возвращает все тарифы из файла: [(url, plan, включён), ...]."""
    path = path or WATCHES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return [(u, p, True) for u, p in DEFAULT_WATCHES]
    items = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print(f"[!] {path}:{n}: ожидается URL|ТАРИФ, пропускаю: {line}", file=sys.stderr)
            continue
        enabled = not (len(parts) >= 3 and parts[2].lower() == "off")
        item = (parts[0], parts[1], enabled)
        if item not in items:
            items.append(item)
    return items


def enabled_only(items: list) -> list:
    out = []
    for url, plan, enabled in items:
        if enabled and (url, plan) not in out:
            out.append((url, plan))
    return out


def load_watches(path: str = None) -> list:
    """Возвращает включённые тарифы [(url, plan), ...]."""
    return enabled_only(load_all_watches(path))


# ---------------------------------------------------------------- проверка

def handle_plan(url: str, plan: str, card, state: dict) -> None:
    """Одно уведомление при появлении тарифа. Повторов нет, пока он не пропадёт и не появится снова."""
    st = state.setdefault((url, plan),
                          {"notified": False, "warned_missing": False, "seen": None, "checked": 0.0})
    st["checked"] = time.time()

    if card is None or card[0] is None:
        st["seen"] = None
        if not st["warned_missing"]:
            tg_send(f"Не могу найти тариф {plan} на странице {url} - возможно, изменилась "
                    f"вёрстка, тариф убрали или сайт отдал заглушку (антибот).")
            st["warned_missing"] = True
        print(f"[?] {plan}: карточка не найдена", flush=True)
        return

    st["warned_missing"] = False
    available, price = card
    st["seen"] = (available, price)

    if available:
        if not st["notified"]:
            label = f"{plan} ({price})" if price else plan
            # если отправка не удалась, повторим на следующем круге
            if tg_send(f"🔥 {label} ДОСТУПЕН!\nБеги покупать: {url}"):
                st["notified"] = True
                print(f"[+] {plan}: доступен, уведомление отправлено", flush=True)
    else:
        if st["notified"]:
            tg_send(f"{plan} снова раскупили.")
        st["notified"] = False
        print(time.strftime("%H:%M:%S"), plan, "- нет в наличии", flush=True)


def check_once(watches: list, state: dict, fails: dict, pause: float = 0.0) -> None:
    """Один круг: каждая страница скачивается один раз, даже если на ней несколько тарифов."""
    by_url = {}
    for url, plan in watches:
        by_url.setdefault(url, []).append(plan)

    for i, (url, plans) in enumerate(by_url.items()):
        if i and pause:
            time.sleep(random.uniform(0.5, pause))
        try:
            cards = parse_cards(fetch(url))
            fails[url] = 0
        except Exception as e:
            fails[url] = fails.get(url, 0) + 1
            print(f"[!] {url}: ошибка запроса ({fails[url]}): {e}", file=sys.stderr, flush=True)
            if fails[url] == FAILS_BEFORE_ALERT:
                tg_send(f"Скрипт {FAILS_BEFORE_ALERT} раз подряд не может открыть {url}: {e}")
            continue
        for plan in plans:
            handle_plan(url, plan, cards.get(plan), state)


def sync_watches(prev: list, cur: list, state: dict) -> None:
    """Сообщает о включённых/выключенных тарифах и сбрасывает состояние выключенных."""
    for url, plan in cur:
        if (url, plan) not in prev:
            tg_send(f"Начал следить за {plan}.")
            print(f"[i] включён {plan}", flush=True)
    for item in prev:
        if item not in cur:
            tg_send(f"Перестал следить за {item[1]}.")
            print(f"[i] выключен {item[1]}", flush=True)
            state.pop(item, None)


# ---------------------------------------------------------------- статус по запросу

def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}д {h}ч {m}м"
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def build_status(all_watches: list, state: dict, fails: dict,
                 started: float, last_cycle: float, now: float = None) -> str:
    now = now or time.time()
    on = enabled_only(all_watches)
    off = [plan for _, plan, enabled in all_watches if not enabled]

    lines = [f"✅ Check-Innon v{VERSION} работает",
             f"Аптайм: {fmt_duration(now - started)}"]
    if last_cycle:
        lines.append(f"Последняя проверка: {fmt_duration(now - last_cycle)} назад "
                     f"(проверяю каждые ~{CHECK_EVERY} с)")
    else:
        lines.append("Первая проверка ещё идёт")
    lines.append("")

    if on:
        lines.append("Слежу за:")
        for url, plan in on:
            st = state.get((url, plan))
            if fails.get(url, 0) > 0:
                text = f"⚠ страница не открывается ({fails[url]} ошибок подряд)"
            elif not st or not st.get("checked"):
                text = "ещё не проверен"
            elif st["seen"] is None:
                text = "не найден на странице"
            else:
                available, price = st["seen"]
                text = "ДОСТУПЕН 🔥" if available else "нет в наличии"
                if price:
                    text = f"{price}, {text}"
            lines.append(f"• {plan}: {text}")
    else:
        lines.append("Сейчас ни один тариф не включён.")

    if off:
        lines.append("")
        lines.append("Выключены: " + ", ".join(off))
    return "\n".join(lines)


def wait_and_answer(deadline: float, offset: int, make_status) -> int:
    """
    Вместо обычной паузы слушает Telegram (long polling) до deadline.
    Любое сообщение от владельца (TG_CHAT_ID) получает в ответ статус.
    Сообщения от других чатов игнорируются. Возвращает новый offset.
    """
    err_logged = False
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return offset
        wait = max(1, int(min(remaining, 25)))
        try:
            res = tg_api("getUpdates",
                         {"offset": offset, "timeout": wait, "allowed_updates": '["message"]'},
                         wait + 10).get("result", [])
            err_logged = False
        except Exception as e:
            if not err_logged:
                print(f"[!] getUpdates: {e}", file=sys.stderr, flush=True)
                err_logged = True
            time.sleep(max(0.0, min(remaining, 5)))
            continue
        for upd in res:
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat") or {}
            if str(chat.get("id")) == str(TG_CHAT_ID):
                tg_send(make_status())
                print("[i] отправил статус по запросу", flush=True)


# ---------------------------------------------------------------- режимы запуска

def cmd_test_telegram() -> int:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Не заданы TG_TOKEN и TG_CHAT_ID", file=sys.stderr)
        return 1
    if tg_send(f"Тестовое сообщение от Check-Innon v{VERSION}: всё работает."):
        print("Сообщение отправлено.")
        return 0
    print("Не удалось отправить сообщение. Проверь токен, ID и что ты написал боту /start.",
          file=sys.stderr)
    return 1


def fatal(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    sys.exit(EX_CONFIG)


def main() -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        fatal("Задай переменные окружения TG_TOKEN и TG_CHAT_ID")

    all_watches = load_all_watches()
    watches = enabled_only(all_watches)
    if not watches:
        fatal(f"Нет включённых тарифов ({WATCHES_FILE}). Включи тариф через меню: innon")

    lines = "\n".join(f"• {plan} - {url}" for url, plan in watches)
    if not tg_send(f"Мониторинг запущен (v{VERSION}). Слежу за:\n{lines}\n\n"
                   f"Напиши мне любое сообщение, и я отвечу статусом."):
        sys.exit("Не удалось отправить тестовое сообщение в Telegram. "
                 "Проверь TG_TOKEN и TG_CHAT_ID и что ты написал боту /start.")
    print(f"Check-Innon v{VERSION}. Слежу за:", ", ".join(plan for _, plan in watches), flush=True)

    started = time.time()
    last_cycle = 0.0
    state, fails = {}, {}
    offset = skip_old_updates()

    while True:
        try:
            all_watches = load_all_watches()
            cur = enabled_only(all_watches)
        except Exception as e:
            print(f"[!] не прочитал список тарифов, оставляю прежний: {e}", file=sys.stderr)
            cur = watches
        if cur != watches:
            sync_watches(watches, cur, state)
            watches = cur
        if watches:
            check_once(watches, state, fails, pause=2.0)
        last_cycle = time.time()

        deadline = last_cycle + CHECK_EVERY + random.uniform(0, 3)
        offset = wait_and_answer(
            deadline, offset,
            lambda: build_status(all_watches, state, fails, started, last_cycle))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--test-telegram":
        sys.exit(cmd_test_telegram())
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V"):
        print(VERSION)
        sys.exit(0)
    main()
