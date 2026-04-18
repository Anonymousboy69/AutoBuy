# ─────────────────────────────────────────────
#  CONFIG LOADER
# ─────────────────────────────────────────────
import json
import os
import logging

def load_config():
    """Load configuration from config.json"""
    try:
        with open("config.json", "r") as f:
            config = json.load(f)

        # Validate required keys
        required_keys = ["bot", "crypto", "database", "shop"]
        for key in required_keys:
            if key not in config:
                logging.error(f"config.json missing required section: {key}")
                exit(1)

        bot_keys = ["prefix", "admin_role", "seller_role"]
        for key in bot_keys:
            if key not in config["bot"]:
                logging.error(f"config.json bot section missing: {key}")
                exit(1)

        crypto_keys = ["receiving_address", "payment_timeout", "poll_interval", "ltc_confirmations", "reservation_timeout"]
        for key in crypto_keys:
            if key not in config["crypto"]:
                logging.error(f"config.json crypto section missing: {key}")
                exit(1)

        db_keys = ["file"]
        for key in db_keys:
            if key not in config["database"]:
                logging.error(f"config.json database section missing: {key}")
                exit(1)

        shop_keys = ["restock_rate_limit", "low_stock_threshold"]
        for key in shop_keys:
            if key not in config["shop"]:
                logging.error(f"config.json shop section missing: {key}")
                exit(1)

        # Validate environment variables for sensitive data
        if not os.getenv("BOT_TOKEN"):
            logging.error("BOT_TOKEN not found in .env file")
            exit(1)
        if not os.getenv("WALLET_SEED"):
            logging.error("WALLET_SEED not found in .env file")
            exit(1)

        # Check for either single token or rotating tokens
        has_single_token = bool(os.getenv("BLOCKCYPHER_TOKEN"))
        has_rotating_tokens = any(os.getenv(f"BLOCKCYPHER_TOKEN_{i}") for i in range(1, 6))

        if not has_single_token and not has_rotating_tokens:
            logging.error("BLOCKCYPHER_TOKEN or BLOCKCYPHER_TOKEN_1-5 not found in .env file")
            exit(1)

        return config
    except FileNotFoundError:
        logging.error("config.json not found! Please create it from the template.")
        exit(1)
    except json.JSONDecodeError:
        logging.error("config.json is invalid JSON!")
        exit(1)

CONFIG = load_config()

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TOKEN                     = os.getenv("BOT_TOKEN")
BLOCKCYPHER_TOKEN         = os.getenv("BLOCKCYPHER_TOKEN")
WALLET_SEED               = os.getenv("WALLET_SEED")
WEBHOOK_HOST              = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT              = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_BASE_URL          = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET            = os.getenv("WEBHOOK_SECRET", "").strip()
BLOCKCYPHER_WEBHOOK_EVENT = os.getenv("BLOCKCYPHER_WEBHOOK_EVENT", "tx-confirmation").strip()

# ─────────────────────────────────────────────
#  ROTATING API KEYS
# ─────────────────────────────────────────────
BLOCKCYPHER_TOKENS = []
for i in range(1, 6):
    token = os.getenv(f"BLOCKCYPHER_TOKEN_{i}")
    if token:
        BLOCKCYPHER_TOKENS.append(token)

# Fallback to single key if no rotating keys set
if not BLOCKCYPHER_TOKENS and BLOCKCYPHER_TOKEN:
    BLOCKCYPHER_TOKENS = [BLOCKCYPHER_TOKEN]

_token_rotation_index = 0

def get_next_blockcypher_token():
    """Get next BlockCypher token in rotation"""
    global _token_rotation_index
    if not BLOCKCYPHER_TOKENS:
        return None
    token = BLOCKCYPHER_TOKENS[_token_rotation_index]
    _token_rotation_index = (_token_rotation_index + 1) % len(BLOCKCYPHER_TOKENS)
    return token

PREFIX            = CONFIG["bot"]["prefix"]
ADMIN_ROLE_ID     = int(CONFIG["bot"]["admin_role"])
SELLER_ROLE_ID    = int(CONFIG["bot"]["seller_role"])
raw_invoice_channel = CONFIG["bot"].get("invoice_channel_id")
INVOICE_CHANNEL_ID = int(raw_invoice_channel) if raw_invoice_channel else None
raw_logging_channel = CONFIG["bot"].get("logging_channel_id")
LOGGING_CHANNEL_ID = int(raw_logging_channel) if raw_logging_channel else None
RECEIVING_ADDRESS = CONFIG["crypto"]["receiving_address"]
DB_FILE           = os.path.abspath(CONFIG["database"]["file"])

# Migrate old root database files into data/ to keep the project root clean.
old_db_file = os.path.abspath("shop_data.db")
old_wallet_file = os.path.abspath("bitcoinlib_wallet.db")
if os.path.exists(old_db_file) and not os.path.exists(DB_FILE):
    os.replace(old_db_file, DB_FILE)
    logging.info(f"Moved existing database from {old_db_file} to {DB_FILE}")
if os.path.exists(old_wallet_file):
    new_wallet_file = os.path.abspath(os.path.join("data", "bitcoinlib_wallet.db"))
    if not os.path.exists(new_wallet_file):
        os.replace(old_wallet_file, new_wallet_file)
        logging.info(f"Moved existing wallet DB from {old_wallet_file} to {new_wallet_file}")