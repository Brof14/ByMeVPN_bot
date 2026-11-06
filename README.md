# ByMeVPN Bot

A Python Telegram bot project for VPN subscriptions and account operations. Its documented integrations include a 3x-ui/Xray panel, YooKassa, SQLite, and FastAPI for webhook handling.

> **Status:** Private project. The feature list below reflects repository documentation and has not been independently checked against a current deployment. Do not assume production readiness from this README.

## Documented workflows

- Telegram onboarding and subscription selection
- VPN access provisioning through 3x-ui
- YooKassa payment processing and webhook handling
- Referral bonuses and renewal reminders
- User self-service and administrator tools
- Background checks for expiring subscriptions

## Stack

Python 3.11 · aiogram 3 · FastAPI · SQLite · py3xui · Docker Compose

## Development setup

```sh
python -m venv .venv
# Activate the virtual environment, then:
pip install -r requirements.txt
cp .env.example .env
```

Configure the values required by the current checkout in `.env`. Use test credentials until the payment and provisioning paths are reviewed. Check the repository's Docker Compose configuration before starting containerized services.

## Security

Never commit Telegram bot tokens, panel credentials, payment secrets, VPN subscription URLs, user exports, or database files. Rotate credentials that have been exposed in Git history or logs. Review payment idempotency, webhook validation, access provisioning, backups, and administrator permissions before deployment.

## Telegram UI

The V3 UI keeps the bot's payment and subscription logic intact. Button styling and optional custom emoji IDs live in `bot_style.py`; brand image visibility is controlled by `bot_assets.py`. See [Telegram UX architecture and rollout](docs/TELEGRAM_UX_ARCHITECTURE.md).

Premium button icons require eligibility under Telegram's Bot API rules. Set `BOT_PREMIUM_BUTTON_ICONS=1` **only after** the bot owner has Telegram Premium or the bot has an eligible purchased Fragment username. The default `0` keeps buttons working without icons.

## Advertising repost setup

To make the bonus workflow direct users to one exact original message, publish an advertising post in the official `@ByMeVPN` channel and configure `AD_SOURCE_POST_URL=https://t.me/ByMeVPN/<message-id>` in the bot's server environment. This is a public link, not a secret. Users forward that message to their own channel; copy-pasted advertisements do not meet the program's conditions. Admin review checks forwarding attribution before a reward is granted. Without the variable, the bot links to the channel rather than inventing a post ID.
