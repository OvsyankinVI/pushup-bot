# Бот отжиманий

MVP для небольшой группы друзей. Каждый активный участник присылает хотя бы один
Telegram-кружок в день. Бот отмечает факт получения `video_note`, **никогда не вызывает
getFile, не скачивает и не хранит видео или file_id**, не анализирует отжимания.

## Устройство проекта

- `main.py`: FastAPI, lifecycle Telegram Application, health, webhook, cron.
- `app/config.py`: переменные окружения и проверка конфигурации.
- `app/database.py`: простые запросы supabase-py без ORM.
- `app/telegram_bot.py`: команды и обработка кружков.
- `app/summaries.py`: московские даты и тексты сводок.
- `sql/schema.sql`: таблицы, уникальность, внешний ключ, RLS.
- `tests/`: автономные тесты без реальных Telegram/Supabase.

Один процесс uvicorn, без polling, scheduler, очередей, файлового хранилища и workers.
Синхронные запросы Supabase выполняются через `asyncio.to_thread`, чтобы не блокировать
FastAPI. Telegram Application инициализируется при старте и останавливается при завершении.
Webhook обрабатывает updates последовательно и отвечает после обработки. Ошибки API/БД
возвращают 503 для повторной доставки; содержимое исключений и секреты не логируются.

## 1. Подготовка на macOS

Нужен Python 3.11 или новее. В терминале из каталога проекта:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Откройте `.env` в редакторе и заполните значения самостоятельно. Файл игнорируется Git.
Не отправляйте его в чат или репозиторий.

Переменные:

- `TELEGRAM_BOT_TOKEN`: обязательно; токен вашего бота от @BotFather.
- `SUPABASE_URL`: обязательно; URL проекта из настроек Supabase.
- `SUPABASE_KEY`: обязательно; серверный secret key или legacy `service_role`.
  Не используйте publishable/anon key: для публичных ролей таблицы закрыты.
- `CRON_SECRET`: обязательно; длинная случайная строка для заголовка `X-Cron-Secret`.
- `TELEGRAM_WEBHOOK_SECRET`: для production обязательно настройте; 1–256 символов
  `A–Z`, `a–z`, `0–9`, `_`, `-`. Одинаковое значение в Render и setWebhook.
  Пустое значение допускается кодом, но отключает проверку подлинности webhook.
- `ADMIN_TELEGRAM_ID`: числовой ID вашего личного Telegram-аккаунта, не username.
  Можно узнать в данных аккаунта/у доверенного инструмента определения Telegram ID.
  Пустое значение не мешает запуску, но блокирует `/chatid` с сообщением конфигурации.
- `TELEGRAM_CHAT_ID`: числовой ID группы (обычно отрицательный), можно оставить пустым
  до первого `/chatid`. Без него cron вернёт 503, остальные команды работают.

Для секретов можно самостоятельно использовать менеджер паролей. Реальных секретов
в проекте нет. Не включайте DEBUG-логи HTTP-клиентов: URL Telegram содержит токен.

## 2. Supabase

1. Откройте свой проект → SQL Editor → New query.
2. Скопируйте весь `sql/schema.sql` и выполните вручную.
3. Убедитесь, что созданы `public.members` и `public.daily_reports`.
4. Перенесите URL проекта и серверный ключ в `.env` и затем в Environment Render.

SQL можно повторить для той же схемы; это не система миграции произвольных старых таблиц.
RLS включён; anon/authenticated не имеют доступа. Серверный ключ держите только на backend.
Уникальные ограничения гарантируют одну регистрацию на `(chat_id, telegram_user_id)` и
одну отметку на `(chat_id, telegram_user_id, report_date)`, в том числе при гонках запросов.
Повторная отметка выполняется через upsert с `ignore_duplicates=True`.

## 3. Локальный запуск и тесты

```bash
source .venv/bin/activate
python -m pytest -q
python -c 'from main import app; print(app.title)'
uvicorn main:app --host 127.0.0.1 --port 8000
```

Импорт и тесты не требуют секретов и не подключаются к сервисам. **Запуск сервера**
требует заполненной конфигурации и выполняет Telegram getMe при инициализации.
Сервер сам не устанавливает webhook. Проверка в другом терминале:

```bash
curl --fail http://127.0.0.1:8000/
```

Ожидается `{"status":"ok"}`. Это проверка процесса, не доступности Supabase.
Telegram не доставляет webhook на localhost: для сквозного теста нужен публичный HTTPS
URL (например, будущий Render URL). Не запускайте второй экземпляр бота без необходимости.

## 4. Telegram и сообщения группы

Создайте бота через @BotFather, добавьте его в закрытую группу и разрешите отправку сообщений.
Чтобы бот получал обычные кружки участников, отключите Group Privacy через
@BotFather → `/setprivacy` → ваш бот → Disable, затем удалите и снова добавьте бота в группу.
Альтернатива — сделать бота администратором группы. Без этого команды могут работать,
а обычные кружки не будут доставляться. См. [официальный Telegram FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

Команды:

- `/start`: «🏋️ Бот отжиманий на связи!», работает и в личке.
- `/join`: регистрация в текущей группе; повтор обновляет имя и username, активирует участника.
- `/members`: активные участники текущей группы.
- `/today`: отметки текущего московского дня.
- `/chatid`: ID текущей группы, только для `ADMIN_TELEGRAM_ID`.

В личке остальные команды сообщают, что нужна группа. Кружки незарегистрированных,
неактивных участников и сообщения от имени канала/анонимного администратора игнорируются.
Отправляйте команды и кружки от своего аккаунта. Обычное видео не считается кружком.

## 5. Render Web Service

После локальной проверки самостоятельно загрузите проект в свой GitHub-репозиторий.
В Render создайте Web Service, подключите репозиторий и выберите Python.

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/`
- Один экземпляр процесса, без дополнительных workers.

В Environment внесите все семь переменных из `.env.example`; значения не вставляйте в код.
Выберите поддерживаемый Python 3.11+ (при необходимости задайте `PYTHON_VERSION` с точной
версией через настройки Render). `PORT` предоставляет Render.
`TELEGRAM_CHAT_ID` можно добавить после `/chatid`, `ADMIN_TELEGRAM_ID` задайте заранее.
Выполните deploy вручную и проверьте `https://YOUR-SERVICE.onrender.com/`.
Официальная инструкция: [FastAPI на Render](https://render.com/docs/deploy-fastapi).

## 6. Установка webhook вручную

Следующая команда меняет webhook бота; выполните её **сами после успешного deploy**.
В активированном локальном venv с заполненным `.env`:

```bash
export PUBLIC_BASE_URL='https://YOUR-SERVICE.onrender.com'
python - <<'PY'
import asyncio
import os
from dotenv import load_dotenv
from telegram import Bot

load_dotenv('.env')

async def main():
    secret = os.environ['TELEGRAM_WEBHOOK_SECRET']
    if not secret:
        raise SystemExit('Сначала задайте TELEGRAM_WEBHOOK_SECRET')
    async with Bot(os.environ['TELEGRAM_BOT_TOKEN']) as bot:
        ok = await bot.set_webhook(
            url=os.environ['PUBLIC_BASE_URL'].rstrip('/') + '/telegram/webhook',
            secret_token=secret,
            allowed_updates=['message'],
            max_connections=1,
        )
        print('Webhook установлен:', ok)

asyncio.run(main())
PY
```

Секрет передаётся Telegram как `secret_token`, а входящие запросы Telegram содержат
`X-Telegram-Bot-Api-Secret-Token`. Неверный/отсутствующий заголовок при настроенном секрете
даёт 403. Плохой JSON даёт 400, ошибки обработки — 503.
См. [Telegram setWebhook](https://core.telegram.org/bots/api#setwebhook).

После настройки отправьте `/start`, `/chatid` от своего аккаунта в группе. Перенесите ответ
в `TELEGRAM_CHAT_ID` в Render и примените настройки. Затем `/join`, кружок, `/today`.
Повторите кружок: бот промолчит, строка в daily_reports останется одна.
Подтверждение отправляется только при фактической вставке новой отметки.

## 7. Внешний cron

В процессе бота нет scheduler. Позднее настройте внешний сервис с HTTPS POST и заголовком
`X-Cron-Secret: <значение CRON_SECRET>`. Не передавайте секрет в query string.

- `POST /cron/evening`: каждый день в **21:00 Europe/Moscow**, сводка за сегодня.
- `POST /cron/midnight`: каждый день в **00:00 Europe/Moscow**, итог за вчера.

Для сервиса с расписанием UTC: `0 18 * * *` и `0 21 * * *` соответственно.
При вызове 30 сентября в 00:00 Москвы итог будет за 29 сентября.
Для ручной проверки (каждый вызов отправляет сообщение в группу):

```bash
# Задайте CRON_SECRET локально, не вставляйте его буквальное значение в историю команд.
read -s 'CRON_SECRET?CRON_SECRET: '
echo
curl --fail-with-body -X POST "$PUBLIC_BASE_URL/cron/evening" \
  -H "X-Cron-Secret: $CRON_SECRET"
curl --fail-with-body -X POST "$PUBLIC_BASE_URL/cron/midnight" \
  -H "X-Cron-Secret: $CRON_SECRET"
unset CRON_SECRET
```

`read` выше рассчитан на стандартный macOS zsh. Успех: HTTP 200 с `report_date`.
Неверный секрет: 403; отсутствующий chat_id или ошибка доставки/БД: 503.

## Решения и ограничения MVP

- Дата кружка — **момент обработки по Europe/Moscow**, как в требованиях, а не дата
  сообщения Telegram. Задержанное сообщение после полуночи попадёт в новый день.
- Midnight всегда использует предыдущий календарный день на момент вызова, даже если
  cron запущен вручную днём. Время запуска endpoints не ограничено.
- Повторный cron отправляет сводку повторно. Нет журнала отправок и гарантии exactly-once.
  Если отметка записалась, но подтверждение не доставлено, повторный кружок не вызовет
  повторной отправки подтверждения. Проверить отметку можно через `/today`.
- Сводки используют текущий список активных участников, без исторических снимков состава.
  Новые участники входят в сводку сразу. Автоматического отключения вышедших нет;
  при необходимости вручную установите `active=false` в Supabase.
- Команды из разных групп изолированы по chat_id; автоматические сводки идут только
  в `TELEGRAM_CHAT_ID`. Это не allowlist: бот может регистрировать людей в других группах,
  куда его добавили. Переход group → supergroup меняет ID; автоматической миграции нет.
- Проект для нескольких друзей: без пагинации больших групп и разбиения длинных сообщений.
- Для своевременной доставки нужен постоянно доступный сервис; засыпание/перезапуск хостинга
  задерживает webhook и cron. Нужны мониторинг вызовов cron и повтор при HTTP 503.
- SQL проверяется локальными тестами по контракту; реальные интеграции проверяются после
  ручной настройки. Локальная разработка не выполняет push, deploy, SQL или setWebhook.
