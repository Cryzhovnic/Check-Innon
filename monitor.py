#!/usr/bin/env python3
"""
Check-Innon: следит за тарифами на my.innon.org и шлёт сообщение в Telegram,
когда тариф становится доступен для покупки (одно сообщение, без повторов).

Если написать боту любое сообщение, он ответит статусом: за чем следит,
что сейчас в наличии, как давно проверял, сколько работает и как быстро
отвечают сайт и Telegram.

Три независимых потока, чтобы медленный Telegram не тормозил проверку сайта:
  - проверка: качает страницы и разбирает тарифы (основной поток);
  - опрос: читает сообщения, адресованные боту, и готовит ответы;
  - отправка: берёт сообщения из очереди и отправляет их с повторами.

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
import queue
import random
import re
import sys
import threading
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
SEND_TIMEOUT = 10        # таймаут одной попытки отправки, секунд
SEND_ATTEMPTS = 10       # сколько раз пытаться отправить сообщение из очереди
SEND_RETRY_PAUSE = 3     # пауза между попытками отправки, секунд
POLL_WAIT = 25           # long polling getUpdates, секунд
SLOW_WARN = 5.0          # ответ дольше этого (сек) пишем в лог как медленный
EX_CONFIG = 78           # код выхода "нечего делать", systemd не перезапускает

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}

NAME_RE = re.compile(r"<h3[^>]*>\s*([^<]+?)\s*</h3>")
PRICE_RE = re.compile(r"(\d[\d\s.,]*₽)")

STOP = threading.Event()
OUTBOX: "queue.Queue[str]" = queue.Queue()

# общее состояние, которое читает поток опроса для ответа статусом
SHARED = {
    "all_watches": [],      # [(url, plan, включён)]
    "last_cycle": 0.0,      # когда закончился последний круг проверки
    "cycle_secs": 0.0,      # сколько длился последний круг
    "site_secs": {},        # url -> сколько секунд отвечал сайт
    "tg_secs": None,        # сколько секунд занимала последняя отправка в Telegram
}


# ---------------------------------------------------------------- Telegram

def tg_api(method: str, params: dict, timeout: float) -> dict:
    api = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_send_once(text: str) -> None:
    """Одна попытка отправки. При ошибке бросает исключение."""
    t0 = time.time()
    tg_api("sendMessage",
           {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": "true"},
           SEND_TIMEOUT)
    secs = time.time() - t0
    SHARED["tg_secs"] = secs
    if secs > SLOW_WARN:
        print(f"[!] Telegram отвечает медленно: {secs:.1f} с", file=sys.stderr, flush=True)


def tg_send(text: str) -> bool:
    """Синхронная отправка, до 3 попыток (старт и тест). Возвращает True при успехе."""
    for attempt in range(3):
        try:
            tg_send_once(text)
            return True
        except Exception as e:
            print(f"[!] Telegram, попытка {attempt + 1}/3: {e}", file=sys.stderr)
            time.sleep(2)
    return False


def notify(text: str) -> None:
    """Ставит сообщение в очередь и сразу возвращается: проверка сайта не ждёт Telegram."""
    OUTBOX.put(text)


def sender_loop() -> None:
    """Поток отправки: достаёт сообщения из очереди и отправляет с повторами."""
    while not STOP.is_set():
        try:
            text = OUTBOX.get(timeout=1)
        except queue.Empty:
            continue
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                tg_send_once(text)
                break
            except Exception as e:
                print(f"[!] Telegram, попытка {attempt}/{SEND_ATTEMPTS}: {e}",
                      file=sys.stderr, flush=True)
                if STOP.wait(SEND_RETRY_PAUSE):
                    return
        else:
            print(f"[!] Не удалось отправить, пропускаю: {text[:60]!r}", file=sys.stderr, flush=True)


def skip_old_updates() -> int:
    """Пропускает сообщения, написанные боту до запуска. Возвращает offset для getUpdates."""
    try:
        res = tg_api("getUpdates", {"offset": -1, "timeout": 0}, 15).get("result", [])
        return res[-1]["update_id"] + 1 if res else 0
    except Exception as e:
        print(f"[!] getUpdates (старт): {e}", file=sys.stderr)
        return 0


def poller_loop(make_status) -> None:
    """
    Поток опроса: слушает Telegram (long polling). Любое сообщение от владельца
    (TG_CHAT_ID) получает в ответ статус. Чужие чаты игнорируются.
    """
    offset = skip_old_updates()
    err_logged = False
    while not STOP.is_set():
        try:
            res = tg_api("getUpdates",
                         {"offset": offset, "timeout": POLL_WAIT, "allowed_updates": '["message"]'},
                         POLL_WAIT + 10).get("result", [])
            err_logged = False
        except Exception as e:
            if not err_logged:
                print(f"[!] getUpdates: {e}", file=sys.stderr, flush=True)
                err_logged = True
            if STOP.wait(5):
                return
            continue
        for upd in res:
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat") or {}
            if str(chat.get("id")) == str(TG_CHAT_ID):
                notify(make_status())
                print("[i] запрошен статус", flush=True)


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
            notify(f"Не могу найти тариф {plan} на странице {url} - возможно, изменилась "
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
            # сообщение уходит через очередь с повторами, поэтому помечаем сразу
            notify(f"🔥 {label} ДОСТУПЕН!\nБеги покупать: {url}")
            st["notified"] = True
            print(f"[+] {plan}: доступен, уведомление поставлено в очередь", flush=True)
    else:
        if st["notified"]:
            notify(f"{plan} снова раскупили.")
        st["notified"] = False
        print(time.strftime("%H:%M:%S"), plan, "- нет в наличии", flush=True)


def check_once(watches: list, state: dict, fails: dict, pause: float = 0.0,
               timings: dict = None) -> None:
    """Один круг: каждая страница скачивается один раз, даже если на ней несколько тарифов."""
    by_url = {}
    for url, plan in watches:
        by_url.setdefault(url, []).append(plan)

    for i, (url, plans) in enumerate(by_url.items()):
        if i and pause:
            time.sleep(random.uniform(0.5, pause))
        t0 = time.time()
        try:
            cards = parse_cards(fetch(url))
            fails[url] = 0
        except Exception as e:
            fails[url] = fails.get(url, 0) + 1
            print(f"[!] {url}: ошибка запроса ({fails[url]}): {e}", file=sys.stderr, flush=True)
            if fails[url] == FAILS_BEFORE_ALERT:
                notify(f"Скрипт {FAILS_BEFORE_ALERT} раз подряд не может открыть {url}: {e}")
            continue
        finally:
            secs = time.time() - t0
            if timings is not None:
                timings[url] = secs
            if secs > SLOW_WARN:
                print(f"[!] сайт отвечает медленно: {secs:.1f} с ({url})", file=sys.stderr, flush=True)
        for plan in plans:
            handle_plan(url, plan, cards.get(plan), state)


def sync_watches(prev: list, cur: list, state: dict) -> None:
    """Сообщает о включённых/выключенных тарифах и сбрасывает состояние выключенных."""
    for url, plan in cur:
        if (url, plan) not in prev:
            notify(f"Начал следить за {plan}.")
            print(f"[i] включён {plan}", flush=True)
    for item in prev:
        if item not in cur:
            notify(f"Перестал следить за {item[1]}.")
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


def build_status(all_watches: list, state: dict, fails: dict, started: float,
                 last_cycle: float, now: float = None, site_secs: dict = None,
                 tg_secs: float = None, cycle_secs: float = None) -> str:
    now = now or time.time()
    on = enabled_only(all_watches)

    lines = [f"✅ Check-Innon v{VERSION} работает",
             f"Аптайм: {fmt_duration(now - started)}"]
    if last_cycle:
        lines.append(f"Последняя проверка: {fmt_duration(now - last_cycle)} назад "
                     f"(проверяю каждые ~{CHECK_EVERY} с)")
    else:
        lines.append("Первая проверка ещё идёт")

    speed = []
    if site_secs:
        speed.append(f"сайт отвечает за {max(site_secs.values()):.1f} с")
    if cycle_secs:
        speed.append(f"круг проверки {cycle_secs:.1f} с")
    if tg_secs is not None:
        speed.append(f"Telegram: отправка за {tg_secs:.1f} с")
    if speed:
        lines.append("Скорость: " + ", ".join(speed))
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

    return "\n".join(lines)


# ---------------------------------------------------------------- цикл проверки

def checker_loop(state: dict, fails: dict, started: float, pause: float = 2.0) -> None:
    """Основной поток. Не зависит от скорости Telegram: сообщения лишь ставятся в очередь."""
    watches = enabled_only(SHARED["all_watches"])
    while not STOP.is_set():
        t0 = time.time()
        try:
            all_watches = load_all_watches()
            cur = enabled_only(all_watches)
        except Exception as e:
            print(f"[!] не прочитал список тарифов, оставляю прежний: {e}", file=sys.stderr)
            all_watches, cur = SHARED["all_watches"], watches
        SHARED["all_watches"] = all_watches
        if cur != watches:
            sync_watches(watches, cur, state)
            watches = cur

        timings = {}
        if watches:
            check_once(watches, state, fails, pause=pause, timings=timings)
        SHARED["site_secs"] = timings
        SHARED["cycle_secs"] = time.time() - t0
        SHARED["last_cycle"] = time.time()

        if STOP.wait(CHECK_EVERY + random.uniform(0, 3)):
            return


# ---------------------------------------------------------------- режимы запуска

def cmd_test_telegram() -> int:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Не заданы TG_TOKEN и TG_CHAT_ID", file=sys.stderr)
        return 1
    if tg_send(f"Тестовое сообщение от Check-Innon v{VERSION}: всё работает."):
        print(f"Сообщение отправлено за {SHARED['tg_secs']:.1f} с.")
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
    SHARED["all_watches"] = all_watches

    lines = "\n".join(f"• {plan} - {url}" for url, plan in watches)
    if not tg_send(f"Мониторинг запущен (v{VERSION}). Слежу за:\n{lines}\n\n"
                   f"Напиши мне любое сообщение, и я отвечу статусом."):
        sys.exit("Не удалось отправить тестовое сообщение в Telegram. "
                 "Проверь TG_TOKEN и TG_CHAT_ID и что ты написал боту /start.")
    print(f"Check-Innon v{VERSION}. Слежу за:", ", ".join(plan for _, plan in watches), flush=True)

    started = time.time()
    state, fails = {}, {}

    def make_status() -> str:
        return build_status(SHARED["all_watches"], state, fails, started, SHARED["last_cycle"],
                            site_secs=SHARED["site_secs"], tg_secs=SHARED["tg_secs"],
                            cycle_secs=SHARED["cycle_secs"])

    threading.Thread(target=sender_loop, name="sender", daemon=True).start()
    threading.Thread(target=poller_loop, args=(make_status,), name="poller", daemon=True).start()
    checker_loop(state, fails, started)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--test-telegram":
        sys.exit(cmd_test_telegram())
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V"):
        print(VERSION)
        sys.exit(0)
    main()
