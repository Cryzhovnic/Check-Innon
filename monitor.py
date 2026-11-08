#!/usr/bin/env python3
"""
Следит за страницей https://my.innon.org/epyc-9275f и шлёт сообщение в Telegram,
когда тариф LC-B1 становится доступен для покупки.

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.

Запуск:
    export TG_TOKEN="123456:ABC..."     # токен бота от @BotFather
    export TG_CHAT_ID="123456789"       # твой chat_id
    python3 monitor.py
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

URL = "https://my.innon.org/epyc-9275f"
PLAN = "LC-B1"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

CHECK_EVERY = 10        # секунд между проверками (плюс небольшой джиттер)
REMIND_EVERY = 60       # пока сервер доступен - повторять уведомление раз в N секунд
FAILS_BEFORE_ALERT = 10 # сколько ошибок подряд до уведомления о проблеме

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}


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


def fetch() -> str:
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def plan_status(html: str):
    """
    Возвращает True (в наличии), False (нет в наличии) или None (карточка не найдена).

    Каждый тариф - блок с class="content-box ...". Внутри:
      недоступен -> <a ...>Нет в наличии</a>
      доступен   -> <button wire:click="addToCart">Добавить в корзину</button>
    """
    chunks = html.split('class="content-box')
    name_re = re.compile(r">\s*" + re.escape(PLAN) + r"\s*</h3>")
    for chunk in chunks:
        if not name_re.search(chunk):
            continue
        if "Нет в наличии" in chunk:
            return False
        if 'wire:click="addToCart"' in chunk or "Добавить в корзину" in chunk:
            return True
        return None
    return None


def main() -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        sys.exit("Задай переменные окружения TG_TOKEN и TG_CHAT_ID")

    if not tg_send(f"Мониторинг {PLAN} запущен.\n{URL}"):
        sys.exit("Не удалось отправить тестовое сообщение в Telegram. "
                 "Проверь TG_TOKEN и TG_CHAT_ID и что ты написал боту /start.")
    print(f"Слежу за {PLAN}...", flush=True)

    was_available = False
    last_alert = 0.0
    fails = 0
    warned_missing = False

    while True:
        try:
            status = plan_status(fetch())
            fails = 0

            if status is None:
                if not warned_missing:
                    tg_send(f"Не могу найти карточку {PLAN} на странице - возможно, "
                            f"изменилась вёрстка или страница отдала заглушку (антибот).")
                    warned_missing = True
                print("[?] карточка не найдена")
            else:
                warned_missing = False
                now = time.time()
                if status and (not was_available or now - last_alert >= REMIND_EVERY):
                    if tg_send(f"🔥 {PLAN} (EPYC 9275F, 399 ₽) ДОСТУПЕН!\nБеги покупать: {URL}"):
                        last_alert = now
                        print("[+] доступен, уведомление отправлено", flush=True)
                elif not status:
                    if was_available:
                        tg_send(f"{PLAN} снова раскупили.")
                    print(time.strftime("%H:%M:%S"), "нет в наличии")
                was_available = status

        except Exception as e:
            fails += 1
            print(f"[!] ошибка запроса ({fails}): {e}", file=sys.stderr)
            if fails == FAILS_BEFORE_ALERT:
                tg_send(f"Скрипт {FAILS_BEFORE_ALERT} раз подряд не может открыть страницу: {e}")

        time.sleep(CHECK_EVERY + random.uniform(0, 3))


if __name__ == "__main__":
    main()
