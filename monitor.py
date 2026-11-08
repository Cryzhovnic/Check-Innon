#!/usr/bin/env python3
"""
Check-Innon: следит за тарифами на my.innon.org и шлёт сообщение в Telegram,
когда тариф становится доступен для покупки.

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.

Список тарифов лежит в файле watches.conf рядом со скриптом, по строке на тариф:
    https://my.innon.org/epyc-9275f|LC-B1
    https://my.innon.org/ultra7-265|LC-A1
Строки с # и пустые игнорируются. Управлять списком удобнее через меню: innon

Запуск:
    export TG_TOKEN="123456:ABC..."     # токен бота от @BotFather
    export TG_CHAT_ID="123456789"       # твой числовой Telegram ID
    python3 monitor.py                  # слежение
    python3 monitor.py --list URL       # показать тарифы на странице
    python3 monitor.py --test-telegram  # отправить тестовое сообщение
"""

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

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

CHECK_EVERY = 10         # секунд между кругами проверки (плюс небольшой джиттер)
REMIND_EVERY = 60        # пока тариф доступен - повторять уведомление раз в N секунд
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

def tg_send(text: str) -> bool:
    """Отправляет сообщение, до 3 попыток. Возвращает True при успехе."""
    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=15) as r:
                r.read()
            return True
        except Exception as e:
            print(f"[!] Telegram, попытка {attempt + 1}/3: {e}", file=sys.stderr)
            time.sleep(2)
    return False


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

def load_watches(path: str = None) -> list:
    path = path or WATCHES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return list(DEFAULT_WATCHES)
    watches = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            print(f"[!] {path}:{n}: ожидается URL|ТАРИФ, пропускаю: {line}", file=sys.stderr)
            continue
        url, plan = (x.strip() for x in line.split("|", 1))
        if url and plan and (url, plan) not in watches:
            watches.append((url, plan))
    return watches


# ---------------------------------------------------------------- проверка

def handle_plan(url: str, plan: str, card, state: dict) -> None:
    st = state.setdefault((url, plan),
                          {"available": False, "last_alert": 0.0, "warned_missing": False})

    if card is None or card[0] is None:
        if not st["warned_missing"]:
            tg_send(f"Не могу найти тариф {plan} на странице {url} - возможно, изменилась "
                    f"вёрстка, тариф убрали или сайт отдал заглушку (антибот).")
            st["warned_missing"] = True
        print(f"[?] {plan}: карточка не найдена", flush=True)
        return

    st["warned_missing"] = False
    available, price = card
    now = time.time()

    if available:
        if not st["available"] or now - st["last_alert"] >= REMIND_EVERY:
            label = f"{plan} ({price})" if price else plan
            if tg_send(f"🔥 {label} ДОСТУПЕН!\nБеги покупать: {url}"):
                st["last_alert"] = now
                print(f"[+] {plan}: доступен, уведомление отправлено", flush=True)
    else:
        if st["available"]:
            tg_send(f"{plan} снова раскупили.")
        print(time.strftime("%H:%M:%S"), plan, "- нет в наличии", flush=True)

    st["available"] = available


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


# ---------------------------------------------------------------- режимы запуска

def cmd_list(url: str) -> int:
    """Печатает тарифы страницы: название<TAB>цена<TAB>yes|no|unknown (для меню)."""
    try:
        cards = parse_cards(fetch(url))
    except Exception as e:
        print(f"Не удалось открыть страницу: {e}", file=sys.stderr)
        return 1
    if not cards:
        print("На странице не найдено ни одного тарифа.", file=sys.stderr)
        return 1
    for name, (available, price) in cards.items():
        status = {True: "yes", False: "no", None: "unknown"}[available]
        print(f"{name}\t{price}\t{status}")
    return 0


def cmd_test_telegram() -> int:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Не заданы TG_TOKEN и TG_CHAT_ID", file=sys.stderr)
        return 1
    if tg_send("Тестовое сообщение от Check-Innon: всё работает."):
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

    watches = load_watches()
    if not watches:
        fatal(f"Список тарифов пуст ({WATCHES_FILE}). Добавь тариф через меню: innon")

    lines = "\n".join(f"• {plan} - {url}" for url, plan in watches)
    if not tg_send(f"Мониторинг запущен. Слежу за:\n{lines}"):
        sys.exit("Не удалось отправить тестовое сообщение в Telegram. "
                 "Проверь TG_TOKEN и TG_CHAT_ID и что ты написал боту /start.")
    print("Слежу за:", ", ".join(plan for _, plan in watches), flush=True)

    state, fails = {}, {}
    while True:
        check_once(watches, state, fails, pause=2.0)
        time.sleep(CHECK_EVERY + random.uniform(0, 3))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--list":
        sys.exit(cmd_list(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--test-telegram":
        sys.exit(cmd_test_telegram())
    main()
