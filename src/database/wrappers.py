"""
Database Wrappers Module
========================
Provides wrapper functions for database operations to avoid passing DB_FILE everywhere.
"""

from typing import Optional, List, Dict, Tuple
from decimal import Decimal, ROUND_DOWN
import discord

# Import database functions
from shopbot.database import (
    get_product as _get_product, all_products as _all_products, get_order as _get_order,
    all_products_by_category as _all_products_by_category, get_categories as _get_categories,
    add_category as _add_category, all_orders as _all_orders, get_stock_items as _get_stock_items,
    get_user_wallet as _get_user_wallet, set_user_wallet as _set_user_wallet,
    remove_user_wallet as _remove_user_wallet, get_seller_revenue as _get_seller_revenue,
    record_payout as _record_payout, get_payout_history as _get_payout_history,
    reserve_stock_items as _reserve_stock_items, release_reserved_stock as _release_reserved_stock,
    get_reserved_stock_items_for_order as _get_reserved_stock_items_for_order,
    build_seller_payout_from_pending_stock as _build_seller_payout_from_pending_stock,
    assign_order_stock_to_order as _assign_order_stock_to_order, get_db
)

# Import config
from utils import (
    DB_FILE, ADMIN_ROLE_ID, SELLER_ROLE_ID, CONFIG, RECEIVING_ADDRESS
)


def get_product(product_id: str) -> Optional[dict]:
    return _get_product(DB_FILE, product_id)


def all_products() -> List[dict]:
    return _all_products(DB_FILE)


def get_order(order_id: str) -> Optional[dict]:
    return _get_order(DB_FILE, order_id)


def all_products_by_category(category: str) -> List[dict]:
    return _all_products_by_category(DB_FILE, category)


def get_categories() -> List[dict]:
    return _get_categories(DB_FILE)


def add_category(name: str, emoji: str = "📦", color: int = 0x9B59B6) -> str:
    return _add_category(DB_FILE, name, emoji, color)


def all_orders() -> List[dict]:
    return _all_orders(DB_FILE)


def get_stock_items(product_id: str, status: str = None) -> List[dict]:
    return _get_stock_items(DB_FILE, product_id, status)


def get_visible_stock_items(product_id: str, user: discord.User, roles: List[discord.Role], guild: discord.Guild | None = None, status: str | None = None) -> List[dict]:
    if guild and guild.owner_id == user.id:
        return get_stock_items(product_id, status)
    if any(r.id == ADMIN_ROLE_ID for r in roles):
        return get_stock_items(product_id, status)
    if any(r.id == SELLER_ROLE_ID for r in roles):
        seller_id = str(user.id)
        conn = get_db(DB_FILE)
        c = conn.cursor()
        if status:
            c.execute(
                'SELECT * FROM stock_items WHERE product_id = ? AND status = ? AND restocked_by = ? ORDER BY created_at ASC',
                (product_id, status, seller_id)
            )
        else:
            c.execute(
                'SELECT * FROM stock_items WHERE product_id = ? AND restocked_by = ? ORDER BY created_at ASC',
                (product_id, seller_id)
            )
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows
    return []


def normalize_product_id(product_id: str) -> str:
    if not product_id:
        return product_id
    product_id = product_id.strip()
    if product_id.lower().startswith("product id:"):
        product_id = product_id.split(":", 1)[1].strip()
    elif product_id.lower().startswith("product id"):
        product_id = product_id.split()[-1].strip()
    return product_id


def reserve_stock_items(product_id: str, quantity: int, order_id: str) -> List[dict]:
    return _reserve_stock_items(DB_FILE, product_id, quantity, order_id)


def release_reserved_stock(order_id: str) -> int:
    return _release_reserved_stock(DB_FILE, order_id)


def get_reserved_stock_items_for_order(order_id: str) -> List[dict]:
    return _get_reserved_stock_items_for_order(DB_FILE, order_id)


def get_user_wallet(user_id: str) -> Optional[dict]:
    return _get_user_wallet(DB_FILE, user_id)


def set_user_wallet(user_id: str, ltc_address: str, linked_by_admin: str) -> bool:
    return _set_user_wallet(DB_FILE, user_id, ltc_address, linked_by_admin)


def remove_user_wallet(user_id: str) -> bool:
    return _remove_user_wallet(DB_FILE, user_id)


def get_seller_revenue(seller_id: str, start_date: float = None, end_date: float = None, platform_fee_percent: float = 0.0) -> dict:
    return _get_seller_revenue(DB_FILE, seller_id, start_date, end_date, platform_fee_percent)


def record_payout(seller_id: str, amount_ltc: float, platform_fee_percent: float, txid: str = None, status: str = 'completed') -> str:
    return _record_payout(DB_FILE, seller_id, amount_ltc, platform_fee_percent, txid, status)


def get_payout_history(seller_id: str = None, limit: int = 50) -> List[dict]:
    return _get_payout_history(DB_FILE, seller_id, limit)


def build_seller_payout_from_pending_stock(order: dict) -> tuple[list[tuple[str, Decimal]] | None, str | None]:
    return _build_seller_payout_from_pending_stock(DB_FILE, order, CONFIG['shop'].get('platform_fee_percent', 0.0), RECEIVING_ADDRESS)


def assign_order_stock_to_order(order: dict) -> bool:
    return _assign_order_stock_to_order(DB_FILE, order)


def build_seller_payout_outputs(order: dict) -> tuple[list[tuple[str, Decimal]] | None, str | None]:
    quantity = int(order.get('quantity', 1))
    reserved_items = get_reserved_stock_items_for_order(order['id'])
    if not reserved_items:
        return None, "No reserved stock items assigned to this order."

    if len(reserved_items) != quantity:
        return None, "Reserved stock quantity mismatch for this order. Please verify stock allocation."

    price_per_item = Decimal(str(order['price_ltc'])) / Decimal(str(quantity))
    recipients: dict[str, Decimal] = {}
    for item in reserved_items:
        seller_wallet = item.get('ltc_address')
        if not seller_wallet:
            return None, "One or more sellers for this order are missing a linked wallet."
        recipients[seller_wallet] = recipients.get(seller_wallet, Decimal('0')) + price_per_item

    fee_percent = Decimal(str(CONFIG['shop'].get('platform_fee_percent', 0.0)))
    if fee_percent and fee_percent > 0:
        fee_multiplier = fee_percent / Decimal('100')
        fee_total = sum(recipients.values()) * fee_multiplier
        for address, amount in list(recipients.items()):
            recipients[address] = (amount * (Decimal('1') - fee_multiplier)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        recipients[RECEIVING_ADDRESS] = recipients.get(RECEIVING_ADDRESS, Decimal('0')) + fee_total.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

    return list(recipients.items()), None