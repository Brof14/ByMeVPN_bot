# 🚀 ByMeVPN Bot - Telegram VPN Sales Bot

> **A stable and reliable Telegram bot for selling VPN subscriptions, featuring automated key delivery and a referral system.**

[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-proprietary-red.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-production%20ready-green.svg)](https://github.com)

## ✨ Features

- 🎁 **Automated key delivery** after payment
- 💳 **Multiple payment methods** (Telegram Stars, YooKassa)
- 🎯 **Referral system** (+3 days per referral, +30 days per purchase)
- 🛡️ **Robust error handling** with graceful degradation
- 📊 **Detailed statistics** for users and administrators
- 🔧 **Asynchronous architecture** for high performance
- 📱 **Support for all devices** (iOS, Android, Windows, macOS, Linux)

## 🚀 Quick Start

### 1. Requirements
- Python 3.9+
- SQLite 3
- Access to a 3x-UI panel
- Telegram Bot Token

### 2. Installation
```bash
# Clone the repository
git clone <repository-url>
cd ByMeVPN_bot

# Install dependencies
pip install -r requirements.txt

# Configuration
cp .env.example .env
nano .env  # Enter your details
```

### 3. User Import (IMPORTANT!)
If you have a user backup file (e.g., `bymevpn_users_20260402_0506.csv`):

```bash
# Import users from CSV to the database
python import_users.py
```

**⚠️ Important:** Run this script ONLY ONCE. ...once before launching the bot for the first time, in order to import all users! ### 4. Launch
```bash
# Standard launch
python main.py

# Background mode (Linux)
nohup python main.py > bot.log 2>&1 &
```

## ⚙️ Configuration

### Main parameters (.env)
```env
# Telegram Bot
BOT_TOKEN=your_bot_token
ADMIN_IDS=your_admin_id

# 3x-UI Panel
XUI_HOST=https://your-panel.com/path
XUI_USERNAME=admin
XUI_PASSWORD=password
INBOUND_ID=5

# VLESS / Reality
REALITY_HOST=your-server.com
REALITY_PORT=443
REALITY_SNI=www.microsoft.com
REALITY_FP=firefox
REALITY_PBK=your_public_key
REALITY_SID=your_short_id

# YooKassa (optional)
YOOKASSA_SHOP_ID=your_shop_id
YOOKASSA_SECRET_KEY=your_secret_key
```

## 📋 Project Structure

```
ByMeVPN_bot/
├── 📄 main.py                 # Entry point
├── 📄 config.py               # Configuration
├── 📄 database.py             # Database
├── 📄 xui.py                  # 3x-UI API
├── 📄 subscription.py          # Subscription logic
├── 📄 payments.py             # Payments
├── 📄 webhook.py              # Webhook server
├── 📄 referral_system.py     # Referral system
├── 📄 notifications.py        # Notifications
├── 📄 keyboards.py            # Keyboards
├── 📄 states.py               # FSM states
├── 📄 utils.py                # Utilities
├── 📄 requirements.txt         # Dependencies
│
├── 📁 handlers/               # Handlers
│   ├── start.py            # /start, menu
│   ├── buy.py              # Purchase
│   ├── keys.py             # Keys
│   ├── admin.py # Admin panel
│   ├── referral.py         # Referrals
│   ├── partner.py          # Partners
│   ├── guide.py            # Guides/Instructions
│   ├── legal.py            # Legal info
│   └── fallback.py         # Fallback
│
└── 📁 vpnbot.db              # Database
```

## 🎯 Functionality

### For users
- 🎁 **Trial period**: 3 days free
- 💎 **Paid plans**: 1, 2, or 5 devices for 1, 6, 12, or 24 months
- 💳 **Payment**: Telegram Stars, YooKassa
- 🎯 **Referrals**: +3 days per referral click, +30 days per payment
- 🔑 **Keys**: Automatic generation and delivery
- 📊 **Statistics**: Purchase and referral history

### For administrators
- 📊 **Statistics**: General and detailed
- 👥 **User management**: Search, view, block
- 🔑 **Key management**: Create, edit, delete
- 💰 **Payment management**: History, refunds
- 🎁 **Referrals**: Monitoring, leaderboard, details
- 📢 **Broadcasts**: Mass notifications
- 🧹 **Cleanup**: Removal of expired keys

## 🔧 Technical features

### 🚀 Performance
- **Asynchronous architecture**: Non-blocking operations
- **Connection pooling**: External request optimization
- **Caching**: In-memory storage for frequently accessed data
- **Batch operations**: Group processing

### 🛡️ Reliability
- **Graceful degradation**: Operation during partial failures
- **Retry mechanisms**: Retries with backoff
- **Idempotency**: Duplicate protection
- **Circuit breaker**: Disabling on multiple errors

### 📊 Monitoring
- **Structured logging**: DEBUG, INFO, ERROR levels
- **Performance metrics**: Operation times, resource usage
- **Detailed errors**: Context and stack trace
- **Action auditing**: All operations are logged

## 🧪 Testing

### Running tests
```bash
# Payment flow test
python test_payment_flow.py

# Asynchronous execution check
python test_async.py

# Database performance test
python -c "
import asyncio
from database import check_database_performance
async def test():
result = await check_database_perfor
