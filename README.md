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
