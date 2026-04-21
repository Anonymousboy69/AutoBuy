# ─────────────────────────────────────────────
#  CONSTANTS & HELPERS
# ─────────────────────────────────────────────
from typing import Optional, List, Dict, Tuple
import os
import discord
from discord.ext import commands
from datetime import datetime, timezone
from decimal import Decimal
from .config import CONFIG, ADMIN_ROLE_ID, SELLER_ROLE_ID, OWNER_ID

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────
COLORS = {
    "primary": 0x9B59B6,
    "success": 0x2ECC71,
    "error":   0xE74C3C,
    "warning": 0xF39C12,
    "info":    0x3498DB,
}

STATUS_EMOJI = {
    "pending":     "⏳",
    "queued":      "⏳",
    "paid":        "✅",
    "delivered":   "🎁",
    "expired":     "⏰",
    "canceled":    "❌",
    "refunded":    "💸",
    "failed":      "❌",
    "sweep_failed": "⚠️",
}

ORDER_STATUS_LABELS = {
    "pending":      "Pending payment",
    "queued":       "Queued for stock",
    "paid":         "Paid",
    "delivered":    "Delivered",
    "expired":      "Expired",
    "canceled":     "Canceled",
    "refunded":     "Refunded",
    "failed":       "Delivery failed",
    "sweep_failed": "Sweep failed",
}

PAYMENT_TIMEOUT   = CONFIG["crypto"]["payment_timeout"]
POLL_INTERVAL     = CONFIG["crypto"]["poll_interval"]
INVOICE_REFRESH_INTERVAL = CONFIG["crypto"].get("invoice_refresh_interval", 5)  # Fast refresh for real-time accuracy
LTC_CONFIRMATIONS = CONFIG["crypto"]["ltc_confirmations"]
RESTOCK_RATE_LIMIT = CONFIG["shop"]["restock_rate_limit"]
LOW_STOCK_THRESHOLD = CONFIG["shop"]["low_stock_threshold"]
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8081"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
BLOCKCYPHER_WEBHOOK_EVENT = os.getenv("BLOCKCYPHER_WEBHOOK_EVENT", "tx-confirmation").strip()
MAX_SWEEP_ATTEMPTS = 5
SWEEP_RETRY_BACKOFF = 300
MAX_REFUND_ATTEMPTS = 3
RESTOCKING_STATUS = 'staging'

# ─────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────
def get_expiration_footer(created_at: float, timeout_seconds: int) -> str:
    expires_at = created_at + timeout_seconds
    remaining = int(expires_at - datetime.now(timezone.utc).timestamp())
    if remaining <= 0:
        return 'Expires soon • Live invoice tracking'

    minutes = remaining // 60
    hours = minutes // 60
    if hours >= 1:
        if minutes % 60 == 0:
            return f"Expires in {hours} hour{'s' if hours != 1 else ''} • Live invoice tracking"
        return f"Expires in {hours} hour{'s' if hours != 1 else ''} {minutes % 60} minutes • Live invoice tracking"
    return f"Expires in {minutes} minute{'s' if minutes != 1 else ''} • Live invoice tracking"


def get_order_expiration_footer(created_at: float, timeout_seconds: int) -> str:
    expires_at = created_at + timeout_seconds
    remaining = int(expires_at - datetime.now(timezone.utc).timestamp())
    if remaining <= 0:
        return 'Expires soon • Live order tracking'

    minutes = remaining // 60
    hours = minutes // 60
    if hours >= 1:
        if minutes % 60 == 0:
            return f"Expires in {hours} hour{'s' if hours != 1 else ''} • Live order tracking"
        return f"Expires in {hours} hour{'s' if hours != 1 else ''} {minutes % 60} minutes • Live order tracking"
    return f"Expires in {minutes} minute{'s' if minutes != 1 else ''} • Live order tracking"


def get_payment_poll_interval(created_at: float, now: float | None = None) -> int:
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    age_minutes = (now - created_at) / 60
    if age_minutes < 10:
        return 30
    if age_minutes < 30:
        return 120
    if age_minutes < 60:
        return 300
    if age_minutes < 180:
        return 600
    return 1800


def get_expiration_timestamp(created_at: float, timeout_seconds: int) -> str:
    """Returns Discord's auto-updating relative timestamp format"""
    expires_at_timestamp = int(created_at + timeout_seconds)
    return f"<t:{expires_at_timestamp}:R>"

def mask_wallet_address(address: str) -> str:
    if not address:
        return "No wallet linked."
    if len(address) <= 16:
        return f"`{address}`"
    return f"`{address[:8]}...{address[-8:]}`"

def format_usd(amount: Decimal | float | int) -> str:
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return f"{amount.quantize(Decimal('0.01')):,}"

def user_has_admin_or_seller_role(member: discord.Member) -> bool:
    if member.guild and member.guild.owner_id == member.id:
        return True
    return any(r.id == ADMIN_ROLE_ID or r.id == SELLER_ROLE_ID for r in member.roles)

def owner_check_interaction(interaction: discord.Interaction) -> bool:
    # Check config-defined owner first
    if OWNER_ID is not None:
        return str(interaction.user.id) == str(OWNER_ID)
    # Fallback to server owner
    return interaction.guild and interaction.guild.owner_id == interaction.user.id

def admin_check_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild and interaction.guild.owner_id == interaction.user.id:
        return True
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)

def seller_check_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild and interaction.guild.owner_id == interaction.user.id:
        return True
    return any(r.id == SELLER_ROLE_ID for r in interaction.user.roles)

def admin_or_seller_check_interaction(interaction: discord.Interaction) -> bool:
    return admin_check_interaction(interaction) or seller_check_interaction(interaction)

def owner_or_admin_check_interaction(interaction: discord.Interaction) -> bool:
    """Check if user is owner OR admin (gives both full bot access except /reset)"""
    return owner_check_interaction(interaction) or admin_check_interaction(interaction)

def is_admin():
    async def predicate(ctx):
        member = ctx.author
        guild  = ctx.guild
        if guild and guild.owner_id == member.id:
            return True
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    return commands.check(predicate)

def is_owner_or_admin():
    async def predicate(ctx):
        member = ctx.author
        guild = ctx.guild
        # Check configurable owner first
        if OWNER_ID is not None:
            return str(member.id) == str(OWNER_ID)
        # Fallback to server owner
        if guild and guild.owner_id == member.id:
            return True
        # Check admin role
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    return commands.check(predicate)
