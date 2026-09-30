# ByMeVPN Bot

Modern Telegram bot for selling and managing VPN subscriptions via 3x-ui / Xray panel.

The bot automates the customer flow: onboarding, plan selection, subscription generation, payment processing, delivery of VPN config, referral bonuses, reminders, and admin management.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Aiogram](https://img.shields.io/badge/Aiogram-3.25-2CA5E0)](https://docs.aiogram.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

## Overview

ByMeVPN Bot is a Telegram sales and support bot for a VPN service. It integrates with a 3x-ui panel to create/configure subscriptions, accepts payments through YooKassa, and provides a full self-service funnel for users.

The project is designed to be used as:

- a customer-facing sales bot in Telegram
- a subscription delivery layer for VPN access
- an admin dashboard for users, payments, referrals, and renewals
- a background automation worker for notifications and expiration handling

## Main features

- Telegram-based onboarding and subscription flow
- Integration with 3x-ui for VPN server / inbound management
- Automatic subscription creation and delivery to users
- YooKassa payment processing
- Referral system with bonus days
- Auto-renewal checks for expiring subscriptions
- Notification scheduler for reminders and updates
- Admin tools for user management and monitoring
- Webhook support for external payment callbacks
- Docker and docker-compose support for deployment

## Architecture

The bot is built on top of:

- Python 3.11
- aiogram 3 for Telegram bot logic
- SQLite for local persistence
- FastAPI + Uvicorn for webhook handling
- 3x-ui API integration through py3xui
- optional Docker deployment

The project is organized around a set of routers in `handlers/` plus service modules for payments, subscriptions, and automation.

## Repository structure

```text
ByMeVPN_bot/
├── main.py                  # Bot entry point and startup
├── config.py                # .env configuration loading
├── database.py              # SQLite layer and migrations
├── constants.py             # Pricing and tariff constants
├── xui_client.py            # 3x-ui integration client
├── payments.py              # Payment logic
├── webhook.py               # Telegram / payment webhook server
├── subscription.py          # Subscription lifecycle handling
├── notifications.py         # Reminder and alert scheduler
├── payment_monitor.py       # Payment monitoring worker
├── autorenew.py             # Auto-renewal check worker
├── async_utils.py           # Async helper utilities
├── cache.py                 # Cache helpers
├── keyboards.py             # Telegram keyboard builders
├── states.py                # FSM state definitions
├── utils.py                 # Shared utilities
├── requirements.txt         # Python dependencies
├── .env.example             # Example environment configuration
├── Dockerfile               # Docker image definition
├── docker-compose.yml       # Local deployment example
├── handlers/                # Telegram routers and command handlers
│   ├── __init__.py
│   ├── start.py
│   ├── buy.py
│   ├── keys.py
│   ├── partner.py
│   ├── guide.py
│   ├── legal.py
│   ├── admin.py
│   ├── auth.py
│   ├── fallback.py
│   └── ...
├── tests/                   # Project tests
├── scripts/                 # Helper scripts
├── data/                    # Runtime data directory
├── README.md
├── CHANGES.md
├── PRODUCTION_AUDIT.md
└── ...
```

## Quick start

### Requirements

- Python 3.11+
- Telegram Bot token from @BotFather
- 3x-ui panel with valid credentials
- YooKassa shop credentials (optional if you are only testing the bot flow)
- Access to a server or VPS for running the bot

### 1) Clone the repository

```bash
git clone https://github.com/Brof14/ByMeVPN_bot.git
cd ByMeVPN_bot
```

### 2) Create environment file

```bash
cp .env.example .env
```

Then fill in your values in `.env`.

### 3) Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4) Run the bot

```bash
python main.py
```

This starts:

- Telegram polling bot
- database initialization
- notification scheduler
- payment monitor
- webhook server
- auto-renew worker

## Environment configuration

The project loads variables from `.env` via `python-dotenv`.

A minimal configuration looks like this:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_IDS=123456789

DB_FILE=data/vpnbot.db

XUI_URL=https://your-panel.example.com:2096
XUI_API_URL=http://127.0.0.1:54321
XUI_USERNAME=admin
XUI_PASSWORD=your_panel_password
XUI_INBOUND_IDS=2,3,7,8,10,11
XUI_SUB_PATH=sub

YOOKASSA_SHOP_ID=your_shop_id
YOOKASSA_SECRET_KEY=your_secret_key

WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_URL=https://your-domain.example.com:8080/webhook/telegram

REF_BONUS_DAYS=3
```

### Important notes

- `XUI_API_URL` should point to the address the bot uses to call the 3x-ui API.
- If the bot and panel are on the same machine, `localhost` or `127.0.0.1` is often the right choice.
- `XUI_URL` is typically the public address used in subscription links shown to users.
- `WEBHOOK_URL` is required if you want external payment callbacks to work reliably.

## Docker deployment

You can run the bot in Docker with the included files.

### Build and run

```bash
docker compose up --build -d
```

The compose setup uses:

- `Dockerfile`
- `.env` file injection
- mounted `./data` directory for SQLite persistence

## Payment flow

The project supports a payment funnel with YooKassa and/or Telegram-based flows depending on the configuration and deployment. A typical flow is:

1. User opens the bot
2. Selects a tariff
3. Bot creates a payment request or checkout session
4. Payment is processed by YooKassa
5. Webhook or polling monitor confirms payment
6. Bot creates or activates the VPN subscription in 3x-ui
7. User receives config / access details
8. Renewal and notifications are handled automatically

## Referral system

The bot includes referral mechanics for users and admins, with bonus days granted for:

- referral completion or click-based actions
- successful payments from invited users

The exact details are controlled by environment values and constants in the project.

## Admin capabilities

The admin side of the bot is expected to cover:

- user monitoring
- key / subscription management
- payment checks
- referral analytics
- broadcasts and notifications
- reminder and maintenance tasks

## Security and operational notes

- Keep `.env` out of version control.
- Do not expose your 3x-ui admin credentials in public repositories.
- Use HTTPS for public webhook and panel endpoints.
- Consider running behind a reverse proxy if the bot is deployed to a public server.
- Review logs when configuring payment or panel connectivity.

## Troubleshooting

### Bot does not start

Check:

- `BOT_TOKEN` is set correctly
- required Python packages are installed
- `main.py` is running with the correct environment

### 3x-ui connection fails

Check:

- `XUI_API_URL` is reachable from the bot host
- the 3x-ui API path is correct
- credentials in `.env` are valid

### Payment not confirmed

Check:

- YooKassa credentials and shop ID
- webhook URL / public access
- payment monitor task is active

## Development

For local development:

```bash
python main.py
```

If you want to add or modify bot flow logic, the main places to start are:

- `main.py` — bootstrapping and startup
- `handlers/` — all bot behavior
- `database.py` — persistence and data access
- `payments.py` and `webhook.py` — payment integration
- `xui_client.py` — 3x-ui automation

## Notes

This repository is a production-style Telegram bot focused on VPN sales automation. It is best suited for users who already understand:

- Telegram bot deployment
- 3x-ui panel configuration
- payment gateway setup
- server-side operations and SSL/webhook hosting

If you are setting it up from scratch, be ready to configure the bot, panel, and payment endpoints together.

## Contact / support

This project is maintained in the repository context of the author. For support, use the relevant contact or issue tracker in the project repository.

---

If you want, I can also make a second version of the README — a more “premium commercial” one with badges, screenshots placeholders, and a cleaner product-style presentation for GitHub.
