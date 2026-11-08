# Check-Innon

Скрипт следит за страницей тарифа на [my.innon.org](https://my.innon.org/epyc-9275f) и присылает уведомление в Telegram, когда тариф **LC-B1** (EPYC 9275F, 399 ₽) становится доступен для покупки.

Используется только стандартная библиотека Python 3.8+, ничего устанавливать через pip не нужно.

## Как это работает

Каждый тариф на странице - отдельная карточка. У недоступного тарифа в ней стоит надпись «Нет в наличии», у доступного - кнопка «Добавить в корзину». Скрипт раз в 10-13 секунд скачивает страницу, находит карточку по названию и смотрит, какая из надписей в ней есть.

- При появлении тарифа приходит сообщение, дальше оно повторяется раз в минуту, пока тариф доступен.
- Если тариф снова раскупили, приходит отдельное сообщение.
- Если карточка не найдена (изменилась вёрстка или сайт отдал антибот-заглушку), приходит предупреждение.
- Если страница не открывается 10 раз подряд, тоже приходит предупреждение.

Скрипт только уведомляет. Покупать нужно вручную, поэтому заранее залогинься на сайте и пополни баланс.

## Настройка Telegram

1. Напиши [@BotFather](https://t.me/BotFather), выполни `/newbot` и получи токен (`TG_TOKEN`).
2. Напиши своему новому боту `/start`. Без этого бот не сможет писать тебе первым.
3. Узнай свой числовой ID (`TG_CHAT_ID`) одним из способов:
   - напиши боту [@userinfobot](https://t.me/userinfobot);
   - открой `https://api.telegram.org/bot<ТОКЕН>/getUpdates` и найди `"chat":{"id":...}`.

Для личной переписки `chat_id` равен твоему user ID. Если указать ID группы, уведомления придут в группу.

## Быстрый запуск

```bash
export TG_TOKEN="123456:ABC-твой-токен"
export TG_CHAT_ID="123456789"
python3 monitor.py
```

В Telegram придёт сообщение «Мониторинг LC-B1 запущен». Если оно не пришло, скрипт завершится с ошибкой.

## Установка на свежий Debian 13

Команды выполняются от root.

**1. Установи Python и сертификаты**

```bash
apt update
apt install -y python3 ca-certificates git
```

**2. Создай отдельного пользователя** без прав администратора

```bash
useradd -r -m -d /opt/innon-monitor -s /usr/sbin/nologin monitor
```

**3. Скачай скрипт**

```bash
git clone https://github.com/Cryzhovnic/Check-Innon /tmp/check-innon
cp /tmp/check-innon/monitor.py /opt/innon-monitor/monitor.py
chown monitor:monitor /opt/innon-monitor/monitor.py
rm -rf /tmp/check-innon
```

**4. Сохрани токены в отдельный файл**

```bash
cat > /etc/innon-monitor.env <<'EOF'
TG_TOKEN=123456:ABC-твой-токен
TG_CHAT_ID=123456789
EOF
chmod 600 /etc/innon-monitor.env
```

**5. Проверь вручную**

```bash
set -a; . /etc/innon-monitor.env; set +a
python3 /opt/innon-monitor/monitor.py
```

В Telegram должно прийти стартовое сообщение, а в консоли будут строки «нет в наличии». Останови скрипт через Ctrl+C.

**6. Запусти как службу**, чтобы скрипт работал постоянно и сам поднимался после перезагрузки

```bash
cat > /etc/systemd/system/innon-monitor.service <<'EOF'
[Unit]
Description=Innon LC-B1 stock monitor
After=network-online.target
Wants=network-online.target

[Service]
User=monitor
EnvironmentFile=/etc/innon-monitor.env
ExecStart=/usr/bin/python3 -u /opt/innon-monitor/monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now innon-monitor
```

**7. Управление**

```bash
systemctl status innon-monitor      # состояние
journalctl -u innon-monitor -f      # живой лог
systemctl restart innon-monitor     # перезапуск после правки скрипта
systemctl disable --now innon-monitor   # остановить и убрать из автозапуска
```

## Настройки в скрипте

| Параметр | По умолчанию | Что делает |
|---|---|---|
| `URL` | `https://my.innon.org/epyc-9275f` | Страница с тарифами |
| `PLAN` | `LC-B1` | Название тарифа, за которым следим |
| `CHECK_EVERY` | `10` | Пауза между проверками, секунд (плюс 0-3 с случайно) |
| `REMIND_EVERY` | `60` | Как часто повторять уведомление, пока тариф доступен |
| `FAILS_BEFORE_ALERT` | `10` | Сколько ошибок подряд до предупреждения |

Чтобы следить за другим тарифом, поменяй `PLAN` (например, на `LC-B2`) и при необходимости `URL`.

## Замечания

- Если сервер с мониторингом находится в России, Telegram может не открываться. Используй VPS в Европе или настрой прокси.
- Слишком частые запросы (`CHECK_EVERY` меньше 5 секунд) могут привести к блокировке по IP.
- Токен бота не храни в репозитории. В примерах выше он лежит в `/etc/innon-monitor.env` с правами `600`.
- Разметка разобрана по сохранённым страницам. Если сайт отдаёт анонимным пользователям другую страницу, скрипт напишет «карточка не найдена».
