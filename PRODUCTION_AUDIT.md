# PRODUCTION AUDIT — ByMeVPN Service

**Дата аудита:** 24 сентября 2026  
**Репозиторий:** `https://github.com/Brof14/ByMeVPN_bot`  
**Сервер:** Ubuntu 24.04 LTS (my1.serv.host)  
**Ветка:** `prod-safe/2026-09-23-billing-provisioning`  
**HEAD Commit:** `19f1d2a62ea2c4b2c7f2c5c6b0ff177db00415f8`  
**Статус рабочей копии:** Clean (нет незакоммиченных изменений)

---

## 1. Current Architecture

ByMeVPN представляет собой гибридную инфраструктуру, состоящую из следующих компонентов:

```
                          ┌──────────────────────────┐
                          │   Клиенты (Пользователи)  │
                          └─────────────┬────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             ▼                          ▼                          ▼
     [Telegram Bot]            [Android App]               [VPN Traffic]
      @ByMeVPN_bot             api.parahin.space:8443      bymevpn.duckdns.org:443 / 4434
             │                          │                          │
             ▼                          ▼                          │
       Docker: vpnbot            Docker: bymevpn_api               │
     (aiogram 3.25.0)          (FastAPI / Uvicorn)                 │
             │                          │                          │
             └──────────┬───────────────┘                          │
                        ▼                                          │
            SQLite: vpnbot.db                                      │
            (/opt/ByMeVPN_bot/data)                                │
                        │                                          │
                        ▼ (py3xui REST API)                        │
             3x-ui Service (Systemd, port 9684)                    │
                        │                                          │
             PostgreSQL 16 (xui DB, port 5432)                     │
                        │                                          │
                        ▼                                          ▼
             Xray Core (bin/xray-linux-amd64) ◄────────────────────┘
                  - Inbound 2 (VLESS NL 443)
                  - Inbound 3 (Hysteria NL 4434)
                  - Inbound 5 (VLESS DE 443, remote node)
                  - Inbound 6 (Hysteria DE 4434, remote node)
```

1. **Telegram-бот (`vpnbot`)**:
   - Контейнер Docker с `network_mode: host`.
   - Библиотека `aiogram 3.25.0` в режиме Long Polling (`dp.start_polling`).
   - Webhook-сервер на FastAPI (`webhook.py`, порт 8080) для приема уведомлений платежей YooKassa.
   - Двойной мониторинг платежей (`payment_monitor.py`): каждые 30 секунд опрашивает YooKassa API и CryptoBot API на предмет успешных платежей.

2. **Backend API (`bymevpn_api`)**:
   - Контейнер Docker (FastAPI) на порту 8000.
   - Проксируется через Caddy: `api.parahin.space:8443` -> `127.0.0.1:8000`.
   - Предоставляет REST API для мобильного приложения Android (регистрация, JWT-токены, trial, статус подписки).
   - Использует ту же базу данных SQLite: `/opt/ByMeVPN_bot/data/vpnbot.db`.

3. **3x-ui и Xray Core**:
   - Установлен в `/usr/local/x-ui/`, управляется через systemd (`x-ui.service`).
   - Внутренний API: `http://127.0.0.1:9684/xwi96m86UF1vda0vfp`.
   - Внешний URL подписок: `https://bymevpn.duckdns.org:2096/xwi96m86UF1vda0vfp`.
   - Подписки раздаются по пути `/yay/<sub_id>`.
   - База данных 3x-ui: **PostgreSQL 16** (`postgresql@16-main.service` на localhost, база `xui`), а не SQLite!

4. **Вспомогательные сервисы**:
   - `supportbot` (Docker): служба технической поддержки (`@ByMeVPNSupportBot`).
   - `caddy` (systemd): SSL-терминация для API.

---

## 2. Production Services

| Сервис | Тип | Статус | Порты / Пути | Назначение |
|---|---|---|---|---|
| `vpnbot` | Docker | Active (Up 2 weeks) | Host network, webhook 8080 | Основной Telegram-бот продаж и управления ключами |
| `bymevpn_api` | Docker | Active (Up 3 days) | 127.0.0.1:8000 | REST API для Android-приложения |
| `supportbot` | Docker | Active (Up 2 weeks) | — | Бот поддержки @ByMeVPNSupportBot |
| `x-ui` | Systemd | Active (running) | 127.0.0.1:9684, 2096 | Панель управления VPN и генератор подписок |
| `xray` | Процесс x-ui | Active (running) | 443 (TCP/TLS), 4434 (UDP) | VPN-ядро Xray (VLESS Reality + Hysteria) |
| `postgresql` | Systemd | Active (running) | 127.0.0.1:5432 | База данных панели 3x-ui (`xui`) |
| `caddy` | Systemd | Active (running) | 8443 | Обратный прокси для Android API |

---

## 3. Database Schema

### 3.1. SQLite (`/opt/ByMeVPN_bot/data/vpnbot.db`)

* **`users`** (235 строк):
  - `user_id` INTEGER PRIMARY KEY (Telegram ID или отрицательный ID для пользователей Android App)
  - `referrer_id` INTEGER (ID пригласившего пользователя)
  - `trial_used` INTEGER DEFAULT 0 (флаг использования триала)
  - `total_paid` INTEGER DEFAULT 0 (общая сумма оплат)
  - `email` TEXT UNIQUE
  - `password_hash`, `google_sub`, `email_verified`, `trial_device_hash` (для Android-клиентов)
  - `is_banned`, `ban_reason`, `created`

* **`keys`** (200 строк):
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `user_id` INTEGER NOT NULL (FK -> users)
  - `key` TEXT NOT NULL (хранит Subscription URL вида `https://.../yay/<sub_id>`)
  - `remark` TEXT (имя конфига, введенное пользователем или сгенерированное)
  - `uuid` TEXT (хранит VLESS link или UUID)
  - `short_id` TEXT
  - `days` INTEGER NOT NULL
  - `limit_ip` INTEGER NOT NULL DEFAULT 1 (лимит одновременных устройств)
  - `created` INTEGER NOT NULL
  - `expiry` INTEGER NOT NULL (Unix timestamp окончания)
  - `source` TEXT DEFAULT 'bot' ('bot', 'app_trial', 'admin')
  - `device_id` INTEGER

* **`payments`** (18 строк):
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `user_id` INTEGER NOT NULL (FK -> users)
  - `amount` INTEGER NOT NULL
  - `currency` TEXT NOT NULL ('RUB', 'XTR')
  - `method` TEXT NOT NULL ('yookassa', 'stars', 'cryptobot')
  - `days` INTEGER NOT NULL
  - `created` INTEGER NOT NULL
  - `payload` TEXT (хранит payment_id YooKassa или invoice_payload Stars)
  - `status` TEXT DEFAULT 'success'
  - `tariff` TEXT
  - `devices` INTEGER DEFAULT 1
  - **КРИТИЧЕСКИЙ ДЕФЕКТ:** Отсутствует `UNIQUE(method, payload)` или `UNIQUE(provider, provider_payment_id)`.

* **`yookassa_processed`** (24 строки):
  - `payment_id` TEXT PRIMARY KEY
  - `processed` INTEGER

* **`yookassa_pending`** (5 строк — зависшие платежи!):
  - `payment_id` TEXT PRIMARY KEY
  - `user_id` INTEGER NOT NULL
  - `days` INTEGER NOT NULL
  - `devices` INTEGER NOT NULL
  - `amount_rub` INTEGER NOT NULL
  - `created` INTEGER

* **`crypto_processed`** (0 строк)
* **`crypto_pending`** (9 строк — зависшие инвойсы!):
  - `invoice_id` TEXT PRIMARY KEY
  - `user_id` INTEGER, `days` INTEGER, `devices` INTEGER, `amount_rub` INTEGER, `created` INTEGER

* **`promo_codes`** (22 строки), **`promo_code_uses`** (25 строк)
* **`devices`** (27 строк), **`refresh_tokens`** (39 строк)

### 3.2. PostgreSQL (`xui` database на localhost:5432)

* **`clients`** (57 строк):
  - `id` bigint PRIMARY KEY
  - `email` text UNIQUE (строковый Telegram ID: `'7737080023'`, `'5352436388'` или имя `'Дамир'`)
  - `uuid` text (UUID клиента Xray, например `e18044a9-bc8c-4c40-9807-14ac91830fc3`)
  - `sub_id` text (16 hex-символов, например `e18044a9bc8c4c40`)
  - `limit_ip` bigint (лимит устройств, сейчас у большинства = 5)
  - `expiry_time` bigint (Unix timestamp в миллисекундах)
  - `enable` boolean DEFAULT true

* **`inbounds`** (4 строки):
  - ID 2: VLESS, порт 443, 🇳🇱Netherlands
  - ID 3: Hysteria, порт 4434, 🇳🇱Netherlands-2
  - ID 5: VLESS, порт 443, 🇩🇪Germany (работает Gemini)
  - ID 6: Hysteria, порт 4434, 🇩🇪Germany-2 (работает Gemini)

---

## 4. Payment Flow

В проекте используются 3 провайдера:

### 4.1. YooKassa (`payments.py` + `webhook.py` + `payment_monitor.py`)
1. Пользователь выбирает тариф в боте (`handlers/buy.py`).
2. Генерируется платеж через `create_yookassa_payment`:
   - `Idempotence-Key = f"{user_id}_{int(time.time())}"`
   - `metadata = {"user_id": str(user_id), "days": str(days), "devices": str(devices)}`
   - Отправляется ссылка `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`
3. Уведомление об оплате поступает **двумя путями**:
   - Webhook: `POST /webhook/yookassa` -> повторный запрос в YooKassa API для верификации статуса `succeeded`.
   - Fallback Monitor (`payment_monitor.py`): опрос YooKassa API каждые 30 секунд.
4. После верификации платеж проверяется через `is_yookassa_processed(payment_id)`.
5. Записывается в `yookassa_pending`.
6. Пользователю отправляется сообщение: «Оплата получена! Введите имя конфига...», и бот переводит пользователя в состояние FSM `BuyFlow.waiting_for_config_name`.
7. **Уязвимость:** если пользователь не написал имя конфига или FSM сбросился при рестарте контейнера, подписка не активируется, запись зависает в `yookassa_pending` (сейчас там 5 таких платежей!).

### 4.2. Telegram Stars (`handlers/buy.py`)
1. Пользователь нажимает «Telegram Stars».
2. Бот отправляет инвойс в валюте `XTR` (курс 1:1 к рублям для покрытия комиссии Telegram).
3. `pre_checkout_query` подтверждается автоматически.
4. Сообщение `successful_payment` перехватывается в `on_successful_payment`.
5. Вызывается `ask_config_name()`, который сразу продлевает существующий ключ или создает новый.
6. **Уязвимость:** нет предварительной проверки на дубликат `successful_payment.telegram_payment_charge_id`.

### 4.3. CryptoBot (`payments.py` + `payment_monitor.py`)
1. Создается инвойс через `@send` API (`create_crypto_payment`).
2. Сохраняется в `crypto_pending`.
3. Опрашивается через `CryptoPaymentMonitor` каждые 30 секунд.
4. При статусе `paid` переводится в `waiting_for_config_name`.
5. **Уязвимость:** аналогично YooKassa, зависит от текстового ввода пользователя после оплаты.

---

## 5. Provisioning Flow

Provisioning выполняется через `subscription.py` -> `xui_client.py` -> 3x-ui REST API:

1. Функция `deliver_key(...)`:
   - Проверяет `extend_existing` (по умолчанию `True`).
   - Ищет существующие ключи пользователя в `keys` (`expiry > now - 7 days`).
   - Если ключ найден:
     - Обновляет `keys.expiry` в SQLite (`extend_key`).
     - Вызывает `update_xui_user_expiry(user_id, days)`.
     - Записывает платеж в `payments` (`add_payment`).
   - Если ключ НЕ найден:
     - Вызывает `create_xui_user(user_id, days, limit_ip=limit_ip)`.
     - `create_xui_user` обращается к 3x-ui API.
     - Сохраняет полученный Subscription URL в `keys`.

2. **КРИТИЧЕСКИЙ ДЕФЕКТ В `create_xui_user` (`xui_client.py:223-256`):**
   ```python
   # Шаг 1: Принудительно удалить существующего клиента из инбаундов
   existing_uuid = await _find_client_uuid(api, email)
   if existing_uuid:
       await _api_call_with_retry(api.client.delete, iid, existing_uuid)
   # Шаг 2: Создать нового клиента с новым UUID
   ```
   Если клиент уже существовал в 3x-ui (например, был добавлен вручную или его ключ выпал из SQLite `keys`), вызов `create_xui_user` **безвозвратно удаляет его UUID и создает новый**, ломая VPN-подключение на всех устройствах клиента!

---

## 6. Existing Subscription Flow

Когда существующий клиент продлевает подписку:
1. `subscription.py:ask_config_name` и `deliver_key` ищут ключ в SQLite `keys`.
2. Если ключ найден в SQLite:
   - `keys.expiry` увеличивается на `days * 86400`.
   - `xui_client.update_xui_user_expiry` обновляет `expiry_time` в 3x-ui.
   - **UUID клиента сохраняется неизменным.**
   - Subscription URL остается прежним (`https://.../yay/<sub_id>`).
3. **НО если клиент есть в 3x-ui, но отсутствует в SQLite `keys` (а таких пользователей сейчас 15!):**
   - Бот считает его «новым пользователем».
   - Вызывает `create_xui_user`.
   - Удаляет старый UUID клиента в 3x-ui.
   - Клиент теряет связь до тех пор, пока не обновит подписку вручную.

---

## 7. Device Limits

- **Текущая реализация:**
  - В `constants.py`: `VALID_DEVICE_LIMITS = (1, 2, 5)`.
  - В `buy.py`: хардкод `devices = 5` для всех тарифов Stars, YooKassa и CryptoBot.
  - В `subscription.py`: хардкод `limit_ip = 5`.
  - В 3x-ui: почти у всех 57 клиентов установлено `limit_ip = 5`.
- **Проблема смены лимита при продлении:**
  - `update_xui_user_expiry` в `xui_client.py` обновляет только дату окончания (`expiry_time`), но считывает `current_limit_ip = client.limit_ip` и перезаписывает его старым значением!
  - Если пользователь покупает апгрейд с 2 до 5 или с 5 до 10 устройств, лимит в 3x-ui **не меняется**!
- **Требуемая целевая модель:**
  - Базовый: **2 устройства**
  - Оплачиваемый апгрейд: **5 устройств**
  - Оплачиваемый апгрейд: **10 устройств**
  - Единый конфиг тарифов и лимитов (без размазывания цифр по коду).

---

## 8. Known Bugs & Code Audit Findings

1. **🚨 Force Delete в `create_xui_user`:**
   При вызове `create_xui_user` для пользователя, который уже есть в 3x-ui, происходит `DELETE` старого клиента и генерация нового `UUID` вместо мягкого `UPDATE`.

2. **🚨 Зависание платежей в состоянии `waiting_for_config_name`:**
   Вместо того чтобы сразу выдать/продлить ключ после подтверждения оплаты, система запрашивает у пользователя имя конфига текстовым сообщением. В базе обнаружено **5 зависших платежей YooKassa** и **9 инвойсов CryptoBot**, где пользователи заплатили, но не ввели имя конфига (или перезапустился бот, потеряв FSM), и услуга им не была предоставлена автоматически.

3. **🚨 Отсутствие Unique Constraint в `payments`:**
   В таблице `payments` поле `payload` не имеет ограничения уникальности. При одновременных или повторных вебхуках одна оплата может быть записана дважды или вызвать дублирующее продление.

4. **🚨 Необновляемый `limit_ip` при продлении:**
   Функция `update_xui_user_expiry` не принимает параметр `limit_ip`, поэтому апгрейд количества устройств не применяется к ядру Xray.

5. **🚨 Ошибка 403 с удаленной нодой Germany в 3x-ui:**
   В логах `/var/log/x-ui/3xui.log` каждые 5 секунд фиксируется:
   `remote Germany active inbounds fetch failed: POST panel/api/clients/activeInbounds: HTTP 403: "this API token is not permitted to access this endpoint"`.
   Это говорит о проблеме авторизации между мастер-панелью и немецкой нодой.

6. **🚨 15 клиентов 3x-ui отсутствуют в базе `keys`:**
   44 числовых Telegram-клиента находятся в 3x-ui, но только 28 из них привязаны к записям в SQLite `keys`. Остальные 16 находятся под угрозой удаления UUID при первой попытке оплатить через бота.

---

## 9. Risks

1. **Риск сброса UUID у реальных платящих клиентов:**
   Если запустить автоисправление без сохранения существующего UUID из 3x-ui, клиенты перестанут подключаться к VPN.
2. **Риск потери оплаты при повторных запросах:**
   Если отключить idempotency-guard до создания атомарного механизма в SQLite, повторные вебхуки YooKassa могут начислить лишние месяцы или сбойнуть.
3. **Риск падения 3x-ui при массовых API-вызовах:**
   `py3xui` работает через HTTP к локальной панели, которая синхронизирует удаленную ноду Germany. При частых параллельных запросах возникает таймаут.
4. **Риск потери платежей в Telegram Stars:**
   Telegram не повторяет `successful_payment`, если обработчик упал с исключением без сохранения в БД.

---

## 10. Proposed Changes (План доработок)

1. **Безопасный Provisioning (No-Delete Guarantee):**
   - Переписать `create_xui_user` / `deliver_key`: сначала всегда проверять наличие клиента в 3x-ui по email/ID.
   - Если клиент уже существует в 3x-ui — **никогда не удалять его**, а вызывать `api.client.update` с сохранением существующего `uuid` и `sub_id`, обновляя `expiry_time` и `limit_ip`.
   - Если ключа не было в SQLite `keys`, но он есть в 3x-ui — автоматически восстанавливать запись в `keys` с существующим UUID и ссылкой.

2. **Немедленная доставка без обязательного ввода конфиг-нейма:**
   - После успешной оплаты ключ/продление выдавать **сразу же**. Имя конфига генерировать автоматически по умолчанию (`ByMeVPN_user_...`), а пользователю давать кнопку «Переименовать», если он захочет.
   - Это гарантирует 100% доставку даже при потере контекста FSM.

3. **Идемпотентность и DB-Level Constraints:**
   - Добавить в таблицу `payments` колонки `provider` и `provider_payment_id` с уникальным индексом:
     `CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_id ON payments(provider, provider_payment_id);`
   - Обернуть обработку платежей в атомарную транзакцию SQLite с блокировкой от race conditions.

4. **Единая модель тарифов и устройств (Single Source of Truth):**
   - Создать в `constants.py` структурированный словарь тарифов и устройств:
     ```python
     DEVICE_TIERS = {
         2: {"name": "Базовый (2 устройства)", "multiplier": 1.0},
         5: {"name": "Оптимальный (5 устройств)", "multiplier": 1.4},
         10: {"name": "Семейный (10 устройств)", "multiplier": 2.0},
     }
     ```
   - При продлении и апгрейде передавать новый `limit_ip` в 3x-ui.

5. **Синхронизация DB ↔ 3x-ui (Read-Only Reporter + Safe Import):**
   - Инструмент для безопасного импорта 15 клиентов из 3x-ui в SQLite `keys` без изменения данных в 3x-ui.

---

## 11. Rollback Plan

1. **Резервная копия базы данных:**
   - SQLite `vpnbot.db` копируется в директорию `/opt/ByMeVPN_bot/backups/`.
   - PostgreSQL `xui` дампится через `pg_dump -h 127.0.0.1 -U UrAVyGGg xui > xui_backup.sql`.
   - Откат БД: восстановление файла `vpnbot.db` и восстановление `psql xui < xui_backup.sql`.
2. **Откат кода:**
   - Git commit фиксируется: `19f1d2a62ea2c4b2c7f2c5c6b0ff177db00415f8`.
   - В случае непредвиденных сбоев: `git checkout 19f1d2a62ea2c4b2c7f2c5c6b0ff177db00415f8 && docker restart vpnbot`.
3. **Откат конфигураций:**
   - Все файлы `.env`, `docker-compose.yml`, конфиги systemd сохраняются в бекап-архив.
