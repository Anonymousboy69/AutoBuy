# 🤖 Discord Shop Bot

A full-featured Discord shop bot with LTC (Litecoin) payments via BlockCypher.

## Features

| Feature | Details |
|---|---|
| **Commands** | Both `/slash` and `!prefix` for every command |
| **Products** | Stored in `shop_data.json`, supports stock management |
| **Payments** | Auto-generates unique LTC address per order via BlockCypher |
| **Auto-verification** | Background task polls every 60s for confirmed payments |
| **Order delivery** | Automatically DMs buyer with product details on confirmation |
| **Admin tools** | Add / edit / delete products, view all orders |
| **Order expiry** | Unpaid orders expire after 1 hour |

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your tokens
```

### 3. Discord Bot Setup
1. Go to https://discord.com/developers/applications
2. Create a new application → Bot
3. Enable **Message Content Intent** and **Server Members Intent**
4. Copy the bot token into `.env`
5. Invite the bot with scopes: `bot` + `applications.commands`
   - Required permissions: Send Messages, Embed Links, Read Message History

### 4. BlockCypher (optional but recommended)
- Sign up free at https://accounts.blockcypher.com
- Free tier: 200 requests/hr without token, higher with token
- Add token to `.env`
- Optional: if BlockCypher becomes rate limited or blocked and you have a paid Blockchair API key, add `BLOCKCHAIR_API_KEY=...` to `.env` for a secondary Litecoin balance lookup

### 5. Admin Role
- Create a role named `ShopAdmin` in your Discord server (or change `ADMIN_ROLE` in `.env`)
- Assign it to trusted members

### 6. Run the bot
```bash
python bot.py
```

---

## Commands Reference

### 🛍️ User Commands

| Command | Description |
|---|---|
| `!shop` / `/shop` | Browse all products |
| `!buy <id>` / `/buy` | Purchase a product |
| `!order <id>` / `/order` | Check order status |
| `!myorders` / `/myorders` | View your order history |
| `!help` / `/help` | Show all commands |

### 🔧 Admin Commands (ShopAdmin role required)

| Command | Description |
|---|---|
| `!addproduct <n> <price> <stock> <desc> \| <delivery>` | Add a product |
| `/addproduct` | Add a product (slash form with fields) |
| `!editproduct <id> <field> <value>` | Edit a field (name/price/description/delivery/stock) |
| `/editproduct` | Edit a product (slash form) |
| `!deleteproduct <id>` / `/deleteproduct` | Remove a product |
| `!allorders` / `/allorders` | View all orders |

---

## Payment Flow

```
User runs /buy
    ↓
Bot generates unique LTC address via BlockCypher
    ↓
Order saved to shop_data.json with status: "pending"
    ↓
Bot DMs user the payment address + amount
    ↓
Background task polls every 60s
    ↓
Unconfirmed tx detected → DM user "Payment Detected"
    ↓
1+ confirmations → Order marked "paid", DM delivery details
```

---

## shop_data.json Structure

```json
{
  "products": {
    "<8-char-id>": {
      "name": "Product Name",
      "price_ltc": 0.05,
      "description": "Short description",
      "delivery": "What the buyer receives after payment",
      "stock": -1
    }
  },
  "orders": {
    "<uuid>": {
      "order_id": "...",
      "user_id": "discord_user_id",
      "product_id": "...",
      "price_ltc": 0.05,
      "ltc_address": "L...",
      "status": "pending|paid|expired|refunded",
      "created_at": 1234567890.0,
      "paid_at": null
    }
  }
}
```

---

## Customisation

| Setting | Location | Default |
|---|---|---|
| Command prefix | `.env` → `PREFIX` | `!` |
| Admin role name | `.env` → `ADMIN_ROLE` | `ShopAdmin` |
| Payment timeout | `bot.py` → `PAYMENT_TIMEOUT` | `3600` (1hr) |
| Poll interval | `bot.py` → `POLL_INTERVAL` | `60` seconds |
| Min confirmations | `bot.py` → `LTC_CONFIRMATIONS` | `1` |

---

## Notes

- **Security**: Never commit your `.env` file. Add it to `.gitignore`.
- **Delivery**: The `delivery` field supports multi-line text — put license keys, download links, etc.
- **Stock**: Set `stock` to `-1` for unlimited. Any `0` value shows as "Out of Stock".
- **Slash sync**: Slash commands sync on startup. If they don't appear, wait up to 1 hour for Discord to propagate, or call `/sync` in a test server first.
