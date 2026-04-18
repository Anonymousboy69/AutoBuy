"""
Discord Shop Bot v2.0
=====================
Enhanced version with:
- SQLite database (scalable)
- Product categories
- Audit logging (who restocked what, when)
- Delivery tracking (pending/delivered/failed)
- Shop pagination & search
- Stock indicators (Low/Medium/High/Unlimited)
- Sorting (price, newest, popularity)
- Bulk restock (paste multiple items)
- Duplicate detection
- Rate limiting per user
- Batch delivery with retry logic
- Low stock alerts
- Persistent embed views
- Global stock pool

Requirements: discord.py>=2.3, aiohttp, python-dotenv, bitcoinlib, sqlite3
"""

import io
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import re
import uuid
import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple
from urllib.parse import quote_plus
from collections import defaultdict
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import from modules
from shopbot.database import init_db, get_db, get_product as _get_product, all_products as _all_products, get_order as _get_order, all_products_by_category as _all_products_by_category, get_categories as _get_categories, add_category as _add_category, all_orders as _all_orders, get_stock_items as _get_stock_items, log_audit as _log_audit, check_rate_limit as _check_rate_limit, hash_content, check_duplicate_stock as _check_duplicate_stock, get_user_wallet as _get_user_wallet, set_user_wallet as _set_user_wallet, remove_user_wallet as _remove_user_wallet, get_seller_revenue as _get_seller_revenue, record_payout as _record_payout, get_payout_history as _get_payout_history, reserve_stock_items as _reserve_stock_items, release_reserved_stock as _release_reserved_stock, get_reserved_stock_items_for_order as _get_reserved_stock_items_for_order, build_seller_payout_from_pending_stock as _build_seller_payout_from_pending_stock, assign_order_stock_to_order as _assign_order_stock_to_order
from shopbot.crypto import get_wallet_from_seed, get_payment_wallet, find_address_path_by_address, generate_ltc_address, get_address_balance, litoshi_to_ltc, format_ltc, fetch_ltc_usd_price, sweep_payment
from shopbot.shop import get_stock_status, ShopPage
from src.services.payment_engine import get_address_transactions, process_automatic_refund, process_payment_delivery
from src.services.order_manager import update_invoice_message, refresh_invoice_message, build_order_embed, update_user_order_message, refresh_order_message, refresh_pending_invoice_messages
from src.services.stock_manager import update_stock_message, send_low_stock_alert, notify_next_in_queue
from src.services.tasks import refresh_invoice_timers, check_payments, update_analytics, database_backup, database_maintenance
from src.commands import *
import src.commands.handlers as command_handlers

# Import from utils
from utils import (
    CONFIG, TOKEN, WALLET_SEED, PREFIX, ADMIN_ROLE_ID, SELLER_ROLE_ID,
    INVOICE_CHANNEL_ID, LOGGING_CHANNEL_ID, RECEIVING_ADDRESS, DB_FILE,
    COLORS, STATUS_EMOJI, PAYMENT_TIMEOUT, RESERVATION_TIMEOUT, POLL_INTERVAL,
    LTC_CONFIRMATIONS, RESTOCK_RATE_LIMIT, LOW_STOCK_THRESHOLD, MAX_SWEEP_ATTEMPTS,
    SWEEP_RETRY_BACKOFF, MAX_REFUND_ATTEMPTS, RESTOCKING_STATUS,
    get_expiration_footer, get_expiration_timestamp, get_order_expiration_footer, mask_wallet_address, format_usd,
    user_has_admin_or_seller_role, admin_check_interaction, seller_check_interaction,
    get_next_blockcypher_token,
    WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_BASE_URL, WEBHOOK_SECRET,
    admin_or_seller_check_interaction, is_admin, INVOICE_REFRESH_INTERVAL
)

# Import from UI embeds and views
from ui.embeds import (
    build_invoice_embed, build_live_embed, build_restock_embed, build_no_stock_embed,
    build_wallet_embed, build_seller_wallet_embed,
    default_embed_data, product_to_builder_data,
)
from ui.views import PartialPaymentConfirmView, InvoiceApproveView, OrderCancelView, EmbedBuilderView, start_product_builder, ShopPage, StockItemPage, EmptyStockItemPage, ProductDetailView, ProductSelect, DashboardView, AdminPanelView, RestockView, PaginatedStockView, ManageItemView, RestockPageView, ItemActionView, RestockTriggerView, WalletView
from ui.modals import ConfirmCancelModal, RefundModal, SingleFieldModal, ColorModal, AddFieldModal, ProductCreateModal, EditProductModal, DeleteProductModal, RestockProductModal, AuditProductModal, SetWalletModal, QuantityModal, BuyProductModal, EditItemModal

# Import database wrappers
from src.database import *



async def process_seller_payout(seller_id: str) -> tuple[bool, str]:
    """Wrapper for payout processing"""
    from src.commands.handlers import process_seller_payout as _process_seller_payout
    return await _process_seller_payout(seller_id)


async def process_all_payouts() -> tuple[bool, str]:
    """Wrapper for batch payout processing"""
    from src.commands.handlers import process_all_payouts as _process_all_payouts
    return await _process_all_payouts()


async def notify_admins_out_of_stock(product: dict, product_id: str, order_id: str):
    """Notify admins when out of stock (wrapper)"""
    from src.commands.handlers import notify_admins_out_of_stock as _notify_admins_out_of_stock
    await _notify_admins_out_of_stock(
        product, product_id, order_id, send_log_embed_callback=send_log_embed
    )


async def deliver_order(order: dict, oid: str, force_delivery: bool = False):
    """Wrapper for order delivery"""
    from src.commands.handlers import deliver_order as _deliver_order
    await _deliver_order(
        order, oid, force_delivery,
        fetch_user_callback=bot.fetch_user,
        send_low_stock_alert_callback=send_low_stock_alert,
        notify_admins_out_of_stock_callback=notify_admins_out_of_stock,
        update_stock_message_callback=update_stock_message,
        update_user_order_message_callback=update_user_order_message,
        get_channel_callback=get_channel_by_id,
        get_address_transactions_callback=get_address_transactions,
        litoshi_to_ltc_callback=litoshi_to_ltc
    )

def log_audit(product_id: str, action: str, admin_id: str, admin_name: str,
              item_count: int = 0, details: str = "") -> str:
    return _log_audit(DB_FILE, product_id, action, admin_id, admin_name, item_count, details)

def check_rate_limit(user_id: str, action: str, limit: int, window_seconds: int = 60) -> bool:
    return _check_rate_limit(DB_FILE, user_id, action, limit, window_seconds)

def check_duplicate_stock(product_id: str, content_hash: str) -> bool:
    return _check_duplicate_stock(DB_FILE, product_id, content_hash)

async def ensure_http_global_over() -> None:
    try:
        if hasattr(bot, '_http') and hasattr(bot._http, '_global_over'):
            if type(bot._http._global_over).__name__ == '_MissingSentinel':
                bot._http._global_over = asyncio.Event()
                bot._http._global_over.set()
                logging.info("✅ Patched bot._http._global_over event for HTTP client")
    except Exception as e:
        logging.debug(f"Could not ensure HTTP global over event: {e}")

async def get_channel_by_id(channel_id: str | int | None):
    """Wrapper for channel resolution"""
    from src.commands.handlers import get_channel_by_id as _get_channel_by_id
    return await _get_channel_by_id(channel_id, bot_instance=bot)

# Prevent duplicate concurrent order processing for the same user/product
_pending_order_requests: set[tuple[str, str]] = set()

# ─────────────────────────────────────────────
#  LOGGING HELPER
# ─────────────────────────────────────────────
async def send_log_embed(title: str, description: str = "", fields: dict = None, color: int = None):
    """Wrapper for sending log embeds"""
    from src.commands.handlers import send_log_embed as _send_log_embed
    await _send_log_embed(
        title, description, fields, color,
        logging_channel_id=LOGGING_CHANNEL_ID,
        get_channel_by_id_callback=get_channel_by_id
    )

# ─────────────────────────────────────────────
#  DATABASE INITIALIZATION
# ─────────────────────────────────────────────
# Database initialization is handled by the imported database.init_db function.

logging.info(f"Using database file: {DB_FILE}")
init_db(DB_FILE)

# ─────────────────────────────────────────────
#  WALLET HELPERS (LTC)
# ─────────────────────────────────────────────
_wallet          = None
_payment_wallet  = None

def get_wallet_from_seed():
    if not WALLET_SEED:
        return None
    try:
        from bitcoinlib.mnemonic import Mnemonic
        from bitcoinlib.keys import HDKey
        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(WALLET_SEED)
        key = HDKey.from_seed(seed_bytes, network="litecoin")
        return key
    except Exception as e:
        logging.error(f"Failed to load wallet: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_payment_wallet():
    global _payment_wallet
    if _payment_wallet is not None:
        return _payment_wallet
    try:
        from bitcoinlib.wallets import Wallet
        from bitcoinlib.mnemonic import Mnemonic
        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(WALLET_SEED)
        wallet_name = "shop_ltc_payment_wallet"
        wallet_db_path = os.path.abspath(os.path.join("data", "bitcoinlib_wallet.db")).replace("\\", "/")
        db_uri = f"sqlite:///{wallet_db_path}"
        try:
            _payment_wallet = Wallet(wallet_name, db_uri=db_uri)
            logging.info(f"Opened existing payment wallet '{wallet_name}'.")
        except Exception as exc:
            logging.info(f"Existing wallet '{wallet_name}' not found, creating new one: {exc}")
            _payment_wallet = Wallet.create(
                wallet_name,
                keys=seed_bytes,
                network="litecoin",
                witness_type="segwit",
                db_uri=db_uri,
            )
            logging.info(f"Created new payment wallet '{wallet_name}'.")
        return _payment_wallet
    except Exception as e:
        logging.error(f"Failed to initialize sweep wallet: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_next_address_index() -> int:
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT MAX(address_index) FROM orders')
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None:
        return int(row[0]) + 1
    return 0

def find_address_path_by_address(address: str) -> str | None:
    root = get_wallet_from_seed()
    if root is None or not address:
        return None
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT MAX(address_index), COUNT(*) FROM orders')
    row = c.fetchone()
    conn.close()
    max_index = 0
    if row:
        max_index = int(row[0]) if row[0] is not None else int(row[1] or 0)
    for index in range(max_index + 10):
        try:
            child = root.subkey_for_path(f"m/0/{index}")
            if child.address() == address:
                return f"m/0/{index}"
        except Exception:
            continue
    return None

def litoshi_to_ltc(litoshi: int) -> Decimal:
    return (Decimal(litoshi) / Decimal('1e8')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def format_ltc(amount: Decimal | float | int) -> str:
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    value = amount.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
    if value == 0:
        return "0"
    return format(value, 'f')

async def fetch_ltc_usd_price() -> float | None:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                return float(data.get("litecoin", {}).get("usd", 0))
    except Exception:
        return None

async def sweep_payment(address_path: str, from_address: str, amount_ltc: Decimal, recipients: list[tuple[str, Decimal]] | None = None) -> tuple[bool, str | None]:
    if not WALLET_SEED or (not RECEIVING_ADDRESS and not recipients):
        logging.error("Sweep failed: WALLET_SEED or destination address(es) not configured")
        return False, None
    try:
        from bitcoinlib.mnemonic import Mnemonic
        from bitcoinlib.keys import HDKey
        from bitcoinlib.transactions import Transaction, Input, Output

        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(WALLET_SEED)
        root = HDKey.from_seed(seed_bytes, network="litecoin")
        child_key = root.subkey_for_path(address_path)

        logging.info(f"Derived key for path {address_path}")
        logging.info("sweep_payment v2 loaded: using bitcoinlib Transaction.sign()")

        addr_url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{from_address}?token={get_next_blockcypher_token()}"
        async with aiohttp.ClientSession() as s:
            async with s.get(addr_url) as r:
                if r.status != 200:
                    resp = await r.text()
                    logging.error(f"BlockCypher query failed: {r.status} - {resp[:300]}")
                    return False, None
                addr_data = await r.json()

        balance = addr_data.get('balance', 0)
        logging.info(f"Address balance: {balance} satoshis")

        txrefs = addr_data.get('txrefs', [])
        confirmed_txs = [tx for tx in txrefs if not tx.get('spent') and tx.get('confirmations', 0) >= LTC_CONFIRMATIONS]

        if not confirmed_txs:
            logging.warning("No confirmed transactions")
            return False, None

        inputs = []
        total_satoshis = 0
        
        # Fetch transaction details in parallel with concurrency limit (max 5 concurrent)
        async def fetch_tx_with_semaphore(s, semaphore, tx_ref):
            tx_hash = tx_ref.get('tx_hash')
            async with semaphore:
                tx_url = f"https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}?token={get_next_blockcypher_token()}"
                try:
                    async with s.get(tx_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            return await r.json()
                        else:
                            logging.warning(f"Failed to fetch tx {tx_hash}: {r.status}")
                except Exception as e:
                    logging.warning(f"Error fetching tx {tx_hash}: {e}")
            return None
        
        async with aiohttp.ClientSession() as s:
            semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent requests
            
            # Fetch all tx details concurrently
            tx_details_list = await asyncio.gather(
                *[fetch_tx_with_semaphore(s, semaphore, tx_ref) for tx_ref in confirmed_txs],
                return_exceptions=False
            )
            
            # Process all transaction results
            for tx_data in tx_details_list:
                if not tx_data:
                    continue
                for idx, output in enumerate(tx_data.get('outputs', [])):
                    out_addrs = output.get('addresses', [])
                    if out_addrs and out_addrs[0] == from_address and not output.get('spent_by'):
                        satoshis = output.get('value', 0)
                        inputs.append({
                            'prev_hash': tx_data.get('hash'),
                            'output_index': idx,
                            'output_value': satoshis,
                            'addresses': [from_address],
                            'script_type': output.get('script_type', 'pay-to-witness-pubkey-hash')
                        })
                        total_satoshis += satoshis

        if not inputs:
            logging.warning("No unspent outputs")
            return False, None

        fee_satoshis = max(2200, len(inputs) * 1100)
        outputs: list[dict] = []

        if recipients:
            total_target_satoshis = 0
            for address, amount in recipients:
                amount_decimal = Decimal(amount).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                satoshis = int((amount_decimal * Decimal('1e8')).to_integral_value(rounding=ROUND_DOWN))
                if satoshis <= 0:
                    continue
                outputs.append({
                    'address': address,
                    'satoshis': satoshis,
                    'amount': amount_decimal,
                })
                total_target_satoshis += satoshis

            if not outputs:
                logging.warning("No valid recipient outputs")
                return False, None

            if total_satoshis < total_target_satoshis + fee_satoshis:
                logging.warning("Not enough funds for requested seller payout outputs")
                return False, None

            remainder = total_satoshis - total_target_satoshis - fee_satoshis
            if remainder > 0:
                outputs[0]['satoshis'] += remainder
        else:
            output_satoshis = total_satoshis - fee_satoshis
            if output_satoshis <= 0:
                logging.warning("Insufficient balance after fees")
                return False, None
            outputs.append({'address': RECEIVING_ADDRESS, 'satoshis': output_satoshis})

        tx_inputs = []
        for inp in inputs:
            tx_inputs.append(Input(prev_txid=inp['prev_hash'], output_n=inp['output_index'], value=inp['output_value'], keys=child_key, witness_type='segwit', network='litecoin'))

        tx_outputs = [Output(value=out['satoshis'], address=out['address'], network='litecoin') for out in outputs]
        tx = Transaction(inputs=tx_inputs, outputs=tx_outputs, witness_type='segwit', network='litecoin')

        logging.info("Signing transaction inputs...")
        tx.sign(keys=child_key)

        tx_hex = tx.raw_hex()
        if not tx_hex:
            logging.error("Failed to generate tx hex")
            return False, None

        logging.info("Transaction hex generated, broadcasting...")
        broadcast_url = f"https://api.blockcypher.com/v1/ltc/main/txs/push?token={get_next_blockcypher_token()}"
        async with aiohttp.ClientSession() as s:
            async with s.post(broadcast_url, json={"tx": tx_hex}) as r:
                resp_text = await r.text()
                if r.status not in (200, 201):
                    logging.error(f"Broadcast failed: {r.status}")
                    logging.error(f"    {resp_text[:300]}")
                    return False, None
                result = await r.json() if resp_text else {}

        txid = result.get('tx', {}).get('hash') or result.get('hash')
        if not txid:
            logging.error("No txid in response")
            logging.error(f"Broadcast result: {result}")
            return False, None

        logging.info(f"Sweep broadcast succeeded: {txid}")
        
        # Verify transaction actually exists on blockchain (don't trust API alone)
        logging.info(f"Verifying transaction {txid} on blockchain...")
        await asyncio.sleep(3)  # Wait for transaction to propagate
        
        verify_url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}?token={get_next_blockcypher_token()}"
        try:
            async with aiohttp.ClientSession() as verify_session:
                async with verify_session.get(verify_url, timeout=aiohttp.ClientTimeout(total=5)) as verify_r:
                    if verify_r.status == 200:
                        verify_result = await verify_r.json()
                        if verify_result.get('hash') == txid or verify_result.get('tx', {}).get('hash') == txid:
                            logging.info(f"✅ Transaction {txid} verified on blockchain")
                            return True, txid
                        else:
                            logging.warning(f"Transaction {txid} returned from API but data mismatch on verification")
                            return True, txid  # Still trust it, just log warning
                    else:
                        logging.warning(f"Verification query returned {verify_r.status}, assuming broadcast succeeded")
                        return True, txid  # Give benefit of doubt if verification fails
        except Exception as e:
            logging.warning(f"Could not verify transaction {txid} on blockchain: {e}, but trusting broadcast succeeded")
            return True, txid  # Don't fail just because verification errored

    except Exception as e:
        logging.error(f"Sweep error: {e}")
        import traceback
        traceback.print_exc()
        return False, None


async def update_invoice_message(order: dict, balance_info: dict | None):
    """Wrapper for order_manager.update_invoice_message"""
    from src.services.order_manager import update_invoice_message as _update_invoice_message
    await _update_invoice_message(
        order, balance_info, get_channel_by_id, get_product,
        get_address_transactions, litoshi_to_ltc
    )


async def refresh_invoice_message(order: dict):
    """Wrapper for order_manager.refresh_invoice_message"""
    from src.services.order_manager import refresh_invoice_message as _refresh_invoice_message
    await _refresh_invoice_message(order, get_channel_by_id, get_product)


def build_order_embed(order: dict, product: dict, format_ltc_callback=None) -> discord.Embed:
    """Wrapper for order_manager.build_order_embed"""
    from src.services.order_manager import build_order_embed as _build_order_embed
    from shopbot.crypto import format_ltc as _format_ltc
    callback = format_ltc_callback or _format_ltc
    return _build_order_embed(order, product, callback)


async def refresh_order_message(order: dict):
    """Wrapper for order_manager.refresh_order_message"""
    from src.services.order_manager import refresh_order_message as _refresh_order_message
    await _refresh_order_message(order, get_channel_by_id, get_product, bot, build_order_embed)


async def refresh_pending_invoice_messages() -> int:
    """Wrapper for order_manager.refresh_pending_invoice_messages"""
    from src.services.order_manager import refresh_pending_invoice_messages as _refresh_pending_invoice_messages
    return await _refresh_pending_invoice_messages(all_orders, update_invoice_message)


@tasks.loop(seconds=INVOICE_REFRESH_INTERVAL)
async def refresh_invoice_timers():
    """Wrapper for tasks.refresh_invoice_timers"""
    from src.services.tasks import refresh_invoice_timers as _refresh_invoice_timers
    await _refresh_invoice_timers(all_orders, refresh_invoice_message, refresh_order_message)

# ─────────────────────────────────────────────
#  STOCK HELPERS
# ─────────────────────────────────────────────
def get_stock_status(product_id: str) -> Tuple[int, str]:
    product = get_product(product_id)
    if not product:
        return 0, "❌"
    
    # Calculate available stock from pending stock items only.
    # Pending orders do not reserve items until payment is confirmed.
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = ?', (product_id, 'pending'))
    pending_items = c.fetchone()[0]
    conn.close()
    
    if product["stock"] < 0:  # Unlimited
        return float('inf'), "∞"
    elif pending_items == 0:
        return 0, "🔴"
    elif pending_items <= LOW_STOCK_THRESHOLD:
        return pending_items, "🟡"
    else:
        return pending_items, "🟢"

async def update_user_order_message(order: dict):
    """Wrapper for order_manager.update_user_order_message"""
    from services.order_manager import update_user_order_message as _update_user_order_message
    await _update_user_order_message(order, get_channel_by_id, get_product, bot, format_ltc)

async def update_stock_message(product_id: str):
    """Wrapper for stock_manager.update_stock_message"""
    from services.stock_manager import update_stock_message as _update_stock_message
    await _update_stock_message(product_id, bot)

async def save_item_message_id(item_id: str, product_id: str, channel_id: int, message_id: int):
    """Save the message ID where an item is displayed for later updates."""
    try:
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute(
            'UPDATE stock_items SET message_channel_id = ?, message_id = ? WHERE id = ?',
            (channel_id, message_id, item_id)
        )
        conn.commit()
        conn.close()
        logging.info(f"Saved message ID for item {item_id}: channel={channel_id}, msg={message_id}")
    except Exception as e:
        logging.warning(f"Could not save item message ID: {e}")

async def update_item_embed(item_id: str, product_id: str, new_content: str):
    """Update the embed message for a specific item (uses stored message ID)."""
    try:
        # Retrieve stored message location
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT message_channel_id, message_id FROM stock_items WHERE id = ?', (item_id,))
        row = c.fetchone()
        conn.close()
        
        if not row or not row[0] or not row[1]:
            logging.warning(f"No stored message ID for item {item_id}")
            return False
        
        channel_id, message_id = row[0], row[1]
        
        # Fetch the message
        channel = await get_channel_by_id(channel_id)
        if not channel:
            logging.warning(f"Could not resolve channel {channel_id} for item {item_id}")
            return False
        
        message = await channel.fetch_message(message_id)
        if not message or not message.embeds:
            logging.warning(f"Could not fetch message {message_id} in channel {channel_id}")
            return False
        
        # Update the embed
        em = message.embeds[0].copy()
        
        # Format new content
        if len(new_content) > 1900:
            display_content = new_content[:1897] + '...'
        else:
            display_content = new_content
        
        safe_display = display_content.replace('```', '`\u200b`')
        boxed = f"```\n{safe_display}\n```"
        
        # Preserve the original position of the content field(s)
        insertion_index = None
        other_fields = []
        for field in em.fields:
            if field.name == "Content" or field.name == "Item Info" or field.name.startswith("Content (") or field.name.startswith("Item Info ("):
                if insertion_index is None:
                    insertion_index = len(other_fields)
                continue
            other_fields.append((field.name, field.value, field.inline))

        if insertion_index is None:
            insertion_index = 0

        em.clear_fields()
        for fname, fvalue, finline in other_fields[:insertion_index]:
            em.add_field(name=fname, value=fvalue, inline=finline)

        # Add new content field(s)
        if len(boxed) > 1024:
            chunks = []
            current_chunk = "```\n"
            for line in safe_display.split('\n'):
                if len(current_chunk) + len(line) + 5 > 900:
                    chunks.append(current_chunk + "\n```")
                    current_chunk = "```\n" + line
                else:
                    current_chunk += line + "\n"
            if current_chunk != "```\n":
                chunks.append(current_chunk + "\n```")

            for idx, chunk in enumerate(chunks, 1):
                em.add_field(name=f"Content ({idx}/{len(chunks)})", value=chunk, inline=False)
        else:
            em.add_field(name="Content", value=boxed, inline=False)

        for fname, fvalue, finline in other_fields[insertion_index:]:
            em.add_field(name=fname, value=fvalue, inline=finline)

        await message.edit(embed=em)
        logging.info(f"✅ Updated item embed {item_id}")
        return True
        
    except discord.errors.NotFound:
        logging.warning(f"Message no longer exists for item {item_id}")
        return False
    except Exception as e:
        logging.warning(f"Failed to update item embed: {e}")
        return False

async def find_existing_product_embed(product: dict, channel):
    """Wrapper for finding existing product embeds"""
    return await find_existing_product_embed(product, channel, bot_user=bot.user)

async def send_low_stock_alert(product_id: str):
    """Wrapper for stock_manager.send_low_stock_alert"""
    from services.stock_manager import send_low_stock_alert as _send_low_stock_alert
    await _send_low_stock_alert(product_id, bot)

async def notify_next_in_queue(product_id: str):
    """Wrapper for stock_manager.notify_next_in_queue"""
    from services.stock_manager import notify_next_in_queue as _notify_next_in_queue
    await _notify_next_in_queue(product_id, bot, get_channel_by_id, update_stock_message, build_order_embed)

# ─────────────────────────────────────────────
#  BOT SETUP
# ─────────────────────────────────────────────
intents                 = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ─────────────────────────────────────────────
#  SHOP VIEWS (PAGINATED & SEARCHABLE)
# ─────────────────────────────────────────────





# Register persistent admin panel view before bot starts
try:
    from ui.views import AdminPanelView
    bot.add_view(AdminPanelView())
    logging.info("✅ Registered AdminPanelView for persistent panel buttons")
except Exception as e:
    logging.error(f"❌ Failed to register AdminPanelView at startup: {e}")

# ─────────────────────────────────────────────
#  ADMIN PANEL VIEW
# ─────────────────────────────────────────────










# ─────────────────────────────────────────────
#  RESTOCK COMMANDS
# ─────────────────────────────────────────────
@bot.command(name="restock")
@is_admin()
async def prefix_restock(ctx, product_id: str = ""):
    await command_handlers.prefix_restock(ctx, product_id)


@bot.tree.command(name="restock", description="[Admin] Restock a product with items")
@app_commands.describe(product_id="The product ID to restock")
async def slash_restock(interaction: discord.Interaction, product_id: str):
    await command_handlers.slash_restock(interaction, product_id)

# ─────────────────────────────────────────────
#  PAYMENT POLLING TASK
# ─────────────────────────────────────────────
async def _get_address_balance_wrapper(address: str):
    """Wrapper for get_address_balance with rotating token"""
    return await get_address_balance(address)

async def _get_addresses_balance_wrapper(addresses: list[str]):
    """Wrapper for get_addresses_balance with rotating token"""
    from shopbot.crypto import get_addresses_balance
    return await get_addresses_balance(addresses)

@tasks.loop(seconds=POLL_INTERVAL)
async def check_payments():
    """Wrapper for tasks.check_payments"""
    from src.services.tasks import check_payments as _check_payments
    await _check_payments(
        all_orders, _get_address_balance_wrapper, _get_addresses_balance_wrapper, litoshi_to_ltc,
        update_invoice_message, process_automatic_refund, process_payment_delivery,
        release_reserved_stock, send_log_embed, get_reserved_stock_items_for_order,
        build_seller_payout_outputs, bot
    )


webhook_server_started = False
webhook_runner = None


def _get_blockcypher_webhook_path() -> str:
    return f"/webhook/blockcypher/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook/blockcypher"


async def _webhook_health(request):
    return web.Response(text="ok")


async def _handle_blockcypher_webhook(request):
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    address = payload.get("address")
    if not address:
        addresses = payload.get("addresses")
        if isinstance(addresses, list) and addresses:
            address = addresses[0]

    if not address:
        return web.Response(status=400, text="Missing address")

    logging.info(f"Received BlockCypher webhook for {address}")

    from src.services.tasks import handle_blockcypher_webhook_event

    try:
        processed = await handle_blockcypher_webhook_event(
            address,
            bot,
            update_invoice_message,
            process_automatic_refund,
            process_payment_delivery,
            release_reserved_stock,
            send_log_embed,
            get_reserved_stock_items_for_order,
            build_seller_payout_outputs,
        )
        return web.Response(status=200, text="processed" if processed else "no orders")
    except Exception as e:
        logging.error(f"Webhook handler error for {address}: {e}")
        return web.Response(status=500, text="internal error")


async def start_webhook_server():
    global webhook_server_started, webhook_runner
    if webhook_server_started or not WEBHOOK_BASE_URL:
        return

    app = web.Application()
    app.router.add_get("/webhook/health", _webhook_health)
    app.router.add_post(_get_blockcypher_webhook_path(), _handle_blockcypher_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()

    webhook_runner = runner
    webhook_server_started = True
    logging.info(f"✅ Webhook server started at http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{_get_blockcypher_webhook_path()}")


# ─────────────────────────────────────────────
#  PRODUCT RESTORATION (ON RESTART)
# ─────────────────────────────────────────────
async def _restore_product_embeds_wrapper():
    """Wrapper for product embed restoration"""
    from src.commands.handlers import restore_product_embeds as _restore_product_embeds
    return await _restore_product_embeds(
        get_channel_by_id_callback=get_channel_by_id,
        find_existing_product_embed_callback=find_existing_product_embed
    )

# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    """Bot ready event handler"""
    await on_ready_handler(
        bot_instance=bot,
        restore_product_embeds_callback=_restore_product_embeds_wrapper,
        get_channel_by_id_callback=get_channel_by_id,
        restore_order_cancel_views_callback=restore_order_cancel_views,
        refresh_pending_invoice_messages_callback=refresh_pending_invoice_messages,
        refresh_invoice_timers_callback=refresh_invoice_timers,
        check_payments_callback=check_payments,
        update_analytics_callback=update_analytics,
        database_backup_callback=database_backup,
        database_maintenance_callback=database_maintenance,
        start_webhook_server_callback=start_webhook_server,
        invoice_channel_id=INVOICE_CHANNEL_ID,
        logging_channel_id=LOGGING_CHANNEL_ID
    )

# ─────────────────────────────────────────────
#  ANALYTICS & MAINTENANCE TASKS
# ─────────────────────────────────────────────
@tasks.loop(hours=1)  # Update analytics every hour
async def update_analytics():
    """Wrapper for tasks.update_analytics"""
    from src.services.tasks import update_analytics as _update_analytics
    await _update_analytics()

@tasks.loop(hours=24)  # Daily backup
async def database_backup():
    """Wrapper for tasks.database_backup"""
    from src.services.tasks import database_backup as _database_backup
    await _database_backup()

@tasks.loop(hours=168)  # Weekly maintenance (7 days)
async def database_maintenance():
    """Wrapper for tasks.database_maintenance"""
    from src.services.tasks import database_maintenance as _database_maintenance
    await _database_maintenance()

# ─────────────────────────────────────────────
#  HELP TEXT

async def _send_dashboard_wrapper(target):
    """Wrapper for commands.send_dashboard"""
    from src.commands.handlers import send_dashboard as _send_dashboard
    await _send_dashboard(target)


async def _send_admin_panel_wrapper(target):
    """Wrapper for commands.send_admin_panel"""
    from src.commands.handlers import send_admin_panel as _send_admin_panel
    await _send_admin_panel(target, bot)


async def fetch_wallet_panel_data(user_id: str):
    wallet = get_user_wallet(user_id)
    if not wallet:
        return None, None, None, None, None, None

    balance_info = None
    price_usd = None
    recent_transactions = None
    all_payouts = None
    payout_history = None
    revenue_data = None

    try:
        balance_info = await asyncio.wait_for(
            get_address_balance(wallet['ltc_address']),
            timeout=1.5
        )
    except Exception:
        balance_info = None

    try:
        price_usd = await asyncio.wait_for(fetch_ltc_usd_price(), timeout=1.5)
    except Exception:
        price_usd = None

    try:
        recent_transactions = await asyncio.wait_for(
            get_address_transactions(wallet['ltc_address']),
            timeout=1.5
        )
    except Exception:
        recent_transactions = None

    all_payouts = get_payout_history(user_id, limit=100)
    payout_history = all_payouts[:3]
    revenue_data = get_seller_revenue(user_id, platform_fee_percent=CONFIG['shop'].get('platform_fee_percent', 0.0))

    return balance_info, revenue_data, payout_history, all_payouts, price_usd, recent_transactions





async def send_wallet_panel(target):
    user_id = str(target.user.id) if isinstance(target, discord.Interaction) else str(target.author.id)
    
    if isinstance(target, discord.Interaction):
        await target.response.defer(ephemeral=True)
    
    balance_info = None
    revenue_data = None
    payout_history = None
    all_payouts = None
    price_usd = None
    recent_transactions = None
    
    try:
        balance_info, revenue_data, payout_history, all_payouts, price_usd, recent_transactions = await asyncio.wait_for(
            fetch_wallet_panel_data(user_id), 
            timeout=2.5
        )
    except Exception:
        pass
    
    em = build_seller_wallet_embed(
        user_id,
        balance_info=balance_info,
        revenue_data=revenue_data,
        payout_history=payout_history,
        all_payouts=all_payouts,
        price_usd=price_usd,
        recent_transactions=recent_transactions,
    )
    
    view = WalletView(user_id)
    
    if isinstance(target, discord.Interaction):
        msg = await target.followup.send(embed=em, view=view, ephemeral=True)
    else:
        msg = await target.send(embed=em, view=view)
    
    # Store message for auto-refresh
    asyncio.create_task(_auto_refresh_wallet(msg, user_id))


async def _auto_refresh_wallet(message: discord.Message, user_id: str):
    """Auto-refresh wallet every 30 seconds"""
    retry_count = 0
    max_retries = 3
    
    while retry_count < max_retries:
        try:
            await asyncio.sleep(30)
            
            try:
                balance_info, revenue_data, payout_history, all_payouts, price_usd, recent_transactions = await asyncio.wait_for(
                    fetch_wallet_panel_data(user_id),
                    timeout=2.0
                )
            except Exception as e:
                logging.debug(f"Wallet data fetch failed: {e}")
                retry_count += 1
                continue
            
            em = build_seller_wallet_embed(
                user_id,
                balance_info=balance_info,
                revenue_data=revenue_data,
                payout_history=payout_history,
                all_payouts=all_payouts,
                price_usd=price_usd,
                recent_transactions=recent_transactions,
            )
            
            try:
                await message.edit(embed=em, view=WalletView(user_id))
                retry_count = 0  # Reset on success
            except discord.NotFound:
                break  # Message was deleted
            except discord.Forbidden:
                break  # No permission to edit
            except Exception as e:
                logging.debug(f"Failed to update wallet message: {e}")
                retry_count += 1
                
        except Exception as e:
            logging.debug(f"Auto-refresh wallet error: {e}")
            retry_count += 1


@bot.command(name="dashboard")
async def prefix_dashboard(ctx):
    """Wrapper for commands.prefix_dashboard"""
    from commands import prefix_dashboard as _prefix_dashboard
    await _prefix_dashboard(ctx, bot)

@bot.tree.command(name="dashboard", description="View shop dashboard and browse products")
async def slash_dashboard(interaction: discord.Interaction):
    """Wrapper for commands.slash_dashboard"""
    from commands import slash_dashboard as _slash_dashboard
    await _slash_dashboard(interaction)

@bot.command(name="panel")
async def prefix_panel(ctx):
    """Wrapper for commands.prefix_panel"""
    from commands import prefix_panel as _prefix_panel
    await _prefix_panel(ctx, bot)

@bot.tree.command(name="panel", description="View admin panel")
async def slash_panel(interaction: discord.Interaction):
    """Wrapper for commands.slash_panel"""
    from commands import slash_panel as _slash_panel
    await _slash_panel(interaction, bot)

@bot.command(name="wallet")
async def prefix_wallet(ctx):
    """Wrapper for commands.prefix_wallet"""
    from commands import prefix_wallet as _prefix_wallet
    await _prefix_wallet(ctx, bot)

@bot.tree.command(name="wallet", description="Manage your linked LTC wallet")
async def slash_wallet(interaction: discord.Interaction):
    """Wrapper for commands.slash_wallet"""
    from commands import slash_wallet as _slash_wallet
    await _slash_wallet(interaction, bot)

# ─────────────────────────────────────────────
#  LTC PRICE
# ─────────────────────────────────────────────
async def send_ltc_price(target):
    is_slash = isinstance(target, discord.Interaction)
    price = await fetch_ltc_usd_price()
    if price is None or price <= 0:
        em = discord.Embed(
            title       = "❌ Error",
            description = "Could not fetch LTC price. Try again later.",
            color       = COLORS["error"],
        )
    else:
        em = discord.Embed(
            title       = "💹 LTC Price (Real-time)",
            color       = COLORS["info"],
        )
        em.add_field(name="Current Price", value=f"**${price:.2f} USD**", inline=False)
        em.set_footer(text="Price from CoinGecko • Updated in real-time")

    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)

@bot.command(name="ltc")
async def prefix_ltc(ctx):
    """Wrapper for commands.prefix_ltc"""
    from commands import prefix_ltc as _prefix_ltc
    await _prefix_ltc(ctx)

@bot.tree.command(name="ltc", description="Check real-time LTC price in USD")
async def slash_ltc(interaction: discord.Interaction):
    """Wrapper for commands.slash_ltc"""
    from commands import slash_ltc as _slash_ltc
    await _slash_ltc(interaction)

# ─────────────────────────────────────────────
#  SHOP
# ─────────────────────────────────────────────
async def send_shop(target, sort_by: str = "newest", category: str = None):
    """Wrapper for commands.send_shop"""
    await command_handlers.send_shop(target, sort_by, category)

@bot.command(name="shop")
async def prefix_shop(ctx):
    await send_shop(ctx)

@bot.tree.command(name="shop", description="Browse the shop")
@app_commands.describe(
    sort_by="How to sort: newest, price_asc, price_desc",
    category="Filter by category"
)
async def slash_shop(interaction: discord.Interaction, sort_by: str = "newest", category: str = None):
    await send_shop(interaction, sort_by, category)

# ─────────────────────────────────────────────
#  BUY
# ─────────────────────────────────────────────
async def process_buy(target, product_id: str, quantity: int = 1):
    """Wrapper for commands.process_buy"""
    await command_handlers.process_buy(
        target,
        product_id,
        quantity,
        bot_instance=bot,
        get_channel_callback=get_channel_by_id,
        update_stock_callback=update_stock_message,
        build_order_embed_callback=build_order_embed,
    )

async def handle_checkstock(interaction: discord.Interaction, product_id: str):
    """Wrapper for stock check modal logic."""
    await command_handlers.slash_stockcheck(
        interaction,
        product_id,
        normalize_product_id,
        get_product,
        get_visible_stock_items,
        build_no_stock_embed,
        StockItemPage,
    )

@bot.command(name="buy")
async def prefix_buy(ctx, product_id: str = ""):
    if not product_id:
        await ctx.send("Usage: `!buy <product_id>`")
        return
    await process_buy(ctx, product_id)

@bot.tree.command(name="buy", description="Purchase a product")
@app_commands.describe(product_id="The product ID (see /shop)")
async def slash_buy(interaction: discord.Interaction, product_id: str):
    await process_buy(interaction, product_id)

@bot.command(name="stockcheck")
async def prefix_stockcheck(ctx, product_id: str = ""):
    if not product_id:
        await ctx.send("Usage: `!stockcheck <product_id>`")
        return

    await command_handlers.prefix_stockcheck(
        ctx,
        product_id,
        normalize_product_id,
        get_product,
        get_visible_stock_items,
        build_no_stock_embed,
        StockItemPage,
    )

@bot.tree.command(name="checkstock", description="Check available stock for a product")
@app_commands.describe(product_id="The product ID to check")
async def slash_stockcheck(interaction: discord.Interaction, product_id: str):
    if not admin_or_seller_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
        return

    await command_handlers.slash_stockcheck(
        interaction,
        product_id,
        normalize_product_id,
        get_product,
        get_visible_stock_items,
        build_no_stock_embed,
        StockItemPage,
    )

# ─────────────────────────────────────────────
#  ORDER STATUS
# ─────────────────────────────────────────────
async def send_order_status(target, order_id_prefix: str):
    await command_handlers.send_order_status(target, order_id_prefix, get_db, get_product)

@bot.command(name="order")
async def prefix_order(ctx, order_id: str = ""):
    if not order_id:
        await ctx.send("Usage: `!order <order_id>`")
        return
    await send_order_status(ctx, order_id)

@bot.tree.command(name="order", description="Check order status")
@app_commands.describe(order_id="First 8+ characters of your order ID")
async def slash_order(interaction: discord.Interaction, order_id: str):
    await send_order_status(interaction, order_id)

# ─────────────────────────────────────────────
#  MY ORDERS
# ─────────────────────────────────────────────
async def send_my_orders(target):
    await command_handlers.send_my_orders(target, get_db, get_product)

@bot.command(name="myorders")
async def prefix_myorders(ctx):
    await send_my_orders(ctx)

@bot.tree.command(name="myorders", description="View your order history")
async def slash_myorders(interaction: discord.Interaction):
    await send_my_orders(interaction)

# ─────────────────────────────────────────────
#  ORDER CANCEL HELPERS

async def find_user_order_for_cancel(target, order_id: str = "") -> dict | None:
    return await command_handlers.find_user_order_for_cancel(target, order_id, get_db)

async def send_order_cancel_response(target, content: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = True):
    await command_handlers.send_order_cancel_response(target, content=content, embed=embed, ephemeral=ephemeral)

async def do_cancel_order(target, order_id: str = ""):
    await command_handlers.do_cancel_order(
        target,
        order_id,
        get_db_callback=get_db,
        get_address_balance_callback=get_address_balance,
        update_invoice_message_callback=update_invoice_message,
        refresh_order_message_callback=refresh_order_message,
        update_stock_message_callback=update_stock_message,
        notify_next_in_queue_callback=notify_next_in_queue,
    )

async def restore_order_cancel_views(get_db_callback=None, bot_instance=None) -> int:
    from src.commands.handlers import restore_order_cancel_views as _restore_order_cancel_views
    return await _restore_order_cancel_views(get_db_callback or get_db, bot_instance or bot)

@bot.command(name="cancelorder")
async def prefix_cancelorder(ctx, order_id: str = ""):
    await do_cancel_order(ctx, order_id)

@bot.command(name="cancel_order")
async def prefix_cancel_order(ctx, order_id: str = ""):
    await do_cancel_order(ctx, order_id)

@bot.tree.command(name="cancelorder", description="Cancel your active order")
@app_commands.describe(order_id="The ID of the order to cancel (optional)")
async def slash_cancelorder(interaction: discord.Interaction, order_id: str = ""):
    await do_cancel_order(interaction, order_id)

@bot.tree.command(name="cancel", description="Cancel your active order")
@app_commands.describe(order_id="The ID of the order to cancel (optional)")
async def slash_cancel(interaction: discord.Interaction, order_id: str = ""):
    await do_cancel_order(interaction, order_id)

# ─────────────────────────────────────────────
#  ADMIN: ADD PRODUCT
# ─────────────────────────────────────────────
async def do_add_product(target, name, price_ltc, description, delivery, stock, category=None,
                         embed_data: dict = None, guild=None, send_confirmation: bool = True) -> str | None:
    is_slash = isinstance(target, discord.Interaction)

    if is_slash and not admin_check_interaction(target):
        await target.followup.send("🚫 Admin only.", ephemeral=True)
        return None

    if guild is None:
        guild = target.guild

    if not guild:
        msg = "❌ Could not resolve the server. Make sure the bot is in a server."
        if is_slash:
            await target.followup.send(msg, ephemeral=True)
        else:
            await target.send(msg)
        return None

    if stock is None:
        stock = 0

    pid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).timestamp()

    channel = None
    try:
        channel_name = "".join(
            c if c.isalnum() or c == "-" else ""
            for c in name.lower().replace(" ", "-")
        )[:100]

        channel_category = None
        if category:
            try:
                channel_category = guild.get_channel(int(category))
                if not isinstance(channel_category, discord.CategoryChannel):
                    channel_category = None
            except (ValueError, TypeError):
                channel_category = None

        channel = await guild.create_text_channel(
            f"product-{channel_name}",
            topic=f"Product ID: {pid}",
            category=channel_category,
        )

        if embed_data:
            custom_footer = (embed_data.get("footer_text") or "").strip()
            footer_text   = f"{custom_footer}  •  Product ID: {pid}" if custom_footer else f"Product ID: {pid}"
            footer_icon   = embed_data.get("footer_url") or None

            em = discord.Embed(
                title       = embed_data.get("title") or name,
                description = embed_data.get("description") or None,
                color       = embed_data.get("color", 0x9B59B6),
            )

            if embed_data.get("author_name"):
                em.set_author(
                    name = embed_data["author_name"],
                    url  = embed_data.get("author_url") or "",
                )

            thumb = embed_data.get("thumbnail_url", "")
            if thumb and thumb.startswith("http"):
                em.set_thumbnail(url=thumb)

            img = embed_data.get("image_url", "")
            if img and img.startswith("http"):
                em.set_image(url=img)

            for f in embed_data.get("fields", []):
                em.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))

            em.add_field(name=" Price", value=f"**{price_ltc} LTC**", inline=True)
            if embed_data.get("price_usd") is not None:
                em.add_field(name="💵 USD", value=f"**${embed_data['price_usd']:.2f}**", inline=True)
            em.add_field(name="📦 Stock", value="∞ Unlimited" if stock < 0 else f"{stock} in stock", inline=True)
            em.set_footer(text=footer_text, icon_url=footer_icon)
        else:
            em = discord.Embed(title=f"🎮 {name}", color=0x9B59B6)
            if description:
                em.description = description
            em.add_field(name=" Price", value=f"**{price_ltc} LTC**", inline=True)
            em.add_field(name="📦 Stock", value="∞ Unlimited" if stock < 0 else f"{stock} in stock", inline=True)
            em.add_field(name="📝 Delivery", value=delivery, inline=False)
            em.set_footer(text=f"Product ID: {pid}")

        view      = ProductDetailView(pid)
        embed_msg = await channel.send(embed=em, view=view)

        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO products
                     (id, name, description, category, price_ltc, price_usd, stock, delivery, channel_id, embed_msg_id, created_at, created_by, updated_at, embed_data)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (pid, name, description, category, price_ltc,
                   embed_data.get("price_usd") if embed_data else None,
                   stock, delivery,
                   channel.id, embed_msg.id, now, str(target.user.id if is_slash else target.author.id), now,
                   json.dumps(embed_data) if embed_data else None))
        conn.commit()
        conn.close()

        log_audit(pid, "product_created", str(target.user.id if is_slash else target.author.id),
                  target.user.name if is_slash else target.author.name, 0, f"Created product: {name}")

    except Exception as e:
        logging.error(f"Failed to create product channel: {e}")
        em_error = discord.Embed(
            title       = "⚠️ Channel Creation Failed",
            description = f"Product added but couldn't create channel: {e}",
            color       = COLORS["warning"],
        )
        if is_slash: await target.followup.send(embed=em_error, ephemeral=True)
        else:        await target.send(embed=em_error)
        return False

    em_confirm = discord.Embed(title="Product Created", color=COLORS["success"])
    em_confirm.add_field(name="ID", value=f"`{pid}`", inline=True)
    em_confirm.add_field(name="Name", value=name, inline=True)
    em_confirm.add_field(name="Price", value=f"{price_ltc} LTC", inline=True)
    em_confirm.set_footer(text=f"Use /restock {pid} to add stock")

    if send_confirmation:
        if is_slash: await target.followup.send(embed=em_confirm, ephemeral=True)
        else:        await target.send(embed=em_confirm)
    return pid

async def refresh_product_embed(product: dict, guild: discord.Guild | None = None) -> tuple[bool, str | None]:
    """Refresh the Discord embed for a product after editing it.
    
    This is a best-effort operation - if it fails, the product is still updated in the database.
    We only update if the channel is already cached to avoid discord.py internal errors.
    """
    if not product.get("channel_id") or not product.get("embed_msg_id"):
        logging.warning(f"refresh_product_embed: skipping product {product.get('id')} - missing channel_id ({product.get('channel_id')}) or embed_msg_id ({product.get('embed_msg_id')})")
        return False, None  # Silently skip if no channel/message info
    
    try:
        # Convert to int
        try:
            channel_id = int(product.get("channel_id"))
            msg_id = int(product.get("embed_msg_id"))
        except (ValueError, TypeError):
            return False, None  # Silently skip on invalid IDs

        channel = None
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                logging.info(f"refresh_product_embed: found channel {channel_id} in interaction guild {guild.id}")
            else:
                try:
                    fetch_channel = getattr(guild, 'fetch_channel', None)
                    if callable(fetch_channel):
                        channel = await fetch_channel(channel_id)
                        logging.info(f"refresh_product_embed: fetched channel {channel_id} from interaction guild {guild.id}")
                except Exception as e:
                    logging.warning(f"refresh_product_embed: guild.fetch_channel failed for {channel_id} in guild {guild.id}: {e}")

        if channel is None:
            channel = bot.get_channel(channel_id)
            if channel is None:
                logging.info(f"refresh_product_embed: channel {channel_id} not cached; attempting cache-only lookup")
                channel = await get_channel_by_id(channel_id)

        if channel is None:
            # Search cached guild channels first
            for guild_search in bot.guilds:
                try:
                    channel = guild_search.get_channel(channel_id)
                    if channel:
                        logging.info(f"refresh_product_embed: found channel {channel_id} in cache for guild {guild_search.id}")
                        break
                except Exception:
                    continue

        if channel is None:
            # As a last resort, fetch guild channels from each guild and retry
            for guild_search in bot.guilds:
                try:
                    await guild_search.fetch_channels()
                    channel = guild_search.get_channel(channel_id)
                    if channel:
                        logging.info(f"refresh_product_embed: found channel {channel_id} after fetching guild {guild_search.id}")
                        break
                except Exception as e:
                    logging.debug(f"refresh_product_embed: failed to fetch channels for guild {guild_search.id}: {e}")
                    continue

        if channel is None:
            logging.warning(f"refresh_product_embed: channel {channel_id} not available in bot guilds")
        else:
            try:
                logging.info(f"refresh_product_embed: fetching message {msg_id} from channel {channel_id}")
                msg = await channel.fetch_message(msg_id)
                data = product_to_builder_data(product)
                em = build_live_embed(data, pid=product.get("id", ""))
                await msg.edit(embed=em, view=ProductDetailView(product.get("id", "")))
                logging.info(f"refresh_product_embed: successfully edited message {msg_id} for product {product.get('id')}")
                return True, None
            except Exception as e:
                logging.warning(f"refresh_product_embed: failed to edit message {msg_id} in channel {channel_id}: {e}")

        logging.info(f"refresh_product_embed: searching all accessible channels for product {product.get('id')}")
        all_channels = []
        for guild in bot.guilds:
            all_channels.extend(getattr(guild, 'text_channels', []))
            all_channels.extend(getattr(guild, 'news_channels', []))

        for ch in all_channels:
            try:
                fallback_msg = await find_existing_product_embed(product, ch)
                if not fallback_msg:
                    continue

                logging.info(f"refresh_product_embed: found fallback embed for product {product.get('id')} in channel {ch.id} message {fallback_msg.id}")
                data = product_to_builder_data(product)
                em = build_live_embed(data, pid=product.get("id", ""))
                await fallback_msg.edit(embed=em, view=ProductDetailView(product.get("id", "")))

                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE products SET channel_id = ?, embed_msg_id = ?, updated_at = ? WHERE id = ?',
                          (ch.id, fallback_msg.id, datetime.now(timezone.utc).timestamp(), product.get("id")))
                conn.commit()
                conn.close()
                logging.info(f"refresh_product_embed: updated product channel_id/embed_msg_id for product {product.get('id')}")
                return True, None
            except Exception as fallback_error:
                logging.debug(f"refresh_product_embed: fallback search failed for channel {ch.id}: {fallback_error}")
                continue

        logging.warning(f"refresh_product_embed: could not locate product embed for product {product.get('id')} in accessible channels")
        return False, None
        
    except Exception as e:
        logging.warning(f"refresh_product_embed: unexpected error for product {product.get('id')}: {e}")
        return False, None  # Any other error - skip silently

@bot.command(name="analytics")
@is_admin()
async def analytics_command(ctx, days: int = 7):
    await command_handlers.analytics_command(ctx, days)


@bot.tree.command(name="analytics", description="[Admin] View sales analytics")
@is_admin()
async def slash_analytics(interaction: discord.Interaction, days: int = 7):
    await interaction.response.defer(ephemeral=True)
    await analytics_command(interaction, days)


@bot.command(name="dbhealth")
@is_admin()
async def db_health_command(ctx):
    await command_handlers.db_health_command(ctx)


@bot.tree.command(name="dbhealth", description="[Admin] Check database health")
@is_admin()
async def slash_db_health(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await db_health_command(interaction)


@bot.command(name="updateproduct")
@is_admin()
async def prefix_updateproduct(ctx, product_id: str, field: str, value: str):
    await update_product_fields(ctx, product_id, field, value, refresh_product_embed)

@bot.command(name="editproduct")
@is_admin()
async def prefix_editproduct(ctx, product_id: str):
    await ctx.send(
        "❌ Prefix editproduct cannot open an ephemeral editor. Use `/editproduct {product_id}` instead.",
        delete_after=12,
    )

@bot.tree.command(name="addproduct", description="[Admin] Add a product to the shop")
async def slash_addproduct(interaction: discord.Interaction):
    await command_handlers.slash_addproduct(interaction)

# ─────────────────────────────────────────────
#  ADMIN: AUDIT LOG
# ─────────────────────────────────────────────
async def send_audit_log(target, product_id: str):
    await command_handlers.send_audit_log(target, product_id)

@bot.command(name="audit")
@is_admin()
async def prefix_audit(ctx, product_id: str = ""):
    if not product_id:
        await ctx.send("Usage: `!audit <product_id>`")
        return
    await send_audit_log(ctx, product_id)

@bot.tree.command(name="audit", description="[Admin] View restock audit log for a product")
@app_commands.describe(product_id="The product ID to view audit log for")
async def slash_audit(interaction: discord.Interaction, product_id: str):
    await send_audit_log(interaction, product_id)

# ─────────────────────────────────────────────
#  ADMIN: EDIT PRODUCT
# ─────────────────────────────────────────────
@bot.tree.command(name="editproduct", description="[Admin] Edit a product")
@app_commands.describe(product_id="Product ID")
async def slash_editproduct(interaction: discord.Interaction, product_id: str):
    await command_handlers.slash_editproduct(interaction, product_id)

# ─────────────────────────────────────────────
#  ADMIN: DELETE PRODUCT
# ─────────────────────────────────────────────
async def do_delete_product(target, product_id: str):
    await command_handlers.do_delete_product(target, product_id, get_channel_by_id)

@bot.command(name="deleteproduct")
@is_admin()
async def prefix_deleteproduct(ctx, product_id: str = ""):
    if not product_id:
        await ctx.send("Usage: `!deleteproduct <product_id>`")
        return
    await do_delete_product(ctx, product_id)

@bot.tree.command(name="deleteproduct", description="[Admin] Delete a product")
@app_commands.describe(product_id="Product ID to remove")
async def slash_deleteproduct(interaction: discord.Interaction, product_id: str):
    await do_delete_product(interaction, product_id)

# ─────────────────────────────────────────────
#  ADMIN: MANUAL ORDER INSERT (DEBUG)
# ─────────────────────────────────────────────
@bot.tree.command(name="insertorder", description="[Admin] Manually insert an order (debug only)")
@app_commands.describe(order_id="Order ID", user_id="User ID", product_id="Product ID", ltc_address="LTC Address", price_ltc="Price in LTC")
async def slash_insertorder(interaction: discord.Interaction, order_id: str, user_id: str, product_id: str, ltc_address: str, price_ltc: float):
    await command_handlers.slash_insertorder(interaction, order_id, user_id, product_id, ltc_address, price_ltc)

# ─────────────────────────────────────────────
#  ADMIN: ALL ORDERS
# ─────────────────────────────────────────────
async def send_all_orders(target):
    await command_handlers.send_all_orders(target)

@bot.command(name="allorders")
@is_admin()
async def prefix_allorders(ctx):
    await send_all_orders(ctx)

@bot.tree.command(name="allorders", description="[Admin] View all orders")
async def slash_allorders(interaction: discord.Interaction):
    await send_all_orders(interaction)

# ─────────────────────────────────────────────
#  ADMIN: MANUAL REFUND
# ─────────────────────────────────────────────
@bot.tree.command(name="refund", description="[Admin] Manually process refund for an order")
@app_commands.describe(order_id="Order ID (first 8 characters)", refund_txid="Refund transaction ID", refund_address="Address where refund was sent")
async def slash_refund(interaction: discord.Interaction, order_id: str, refund_txid: str = None, refund_address: str = None):
    await command_handlers.slash_refund(interaction, order_id, refund_txid, refund_address, bot.fetch_user)

@bot.tree.command(name="checkbalance", description="[Admin] Check LTC balance of an order address")
@app_commands.describe(order_id="Order ID (first 8 characters)")
async def slash_checkbalance(interaction: discord.Interaction, order_id: str):
    await command_handlers.slash_checkbalance(interaction, order_id)

@bot.tree.command(name="sellerrevenue", description="[Admin] Check seller revenue from their restocked items")
@app_commands.describe(user="The Discord user to check revenue for", platform_fee="Platform fee percentage (optional, uses config default)")
async def slash_seller_revenue(interaction: discord.Interaction, user: discord.Member, platform_fee: float = None):
    await command_handlers.slash_seller_revenue(interaction, user, platform_fee)

@bot.tree.command(name="payouts", description="[Admin] Manage seller payouts")
@app_commands.describe(action="What to do", user="Specific seller (optional)")
@app_commands.choices(action=[
    app_commands.Choice(name="check", value="check"),
    app_commands.Choice(name="pay", value="pay"),
    app_commands.Choice(name="payall", value="payall"),
    app_commands.Choice(name="enable", value="enable"),
    app_commands.Choice(name="disable", value="disable"),
])
async def slash_payouts(interaction: discord.Interaction, action: str, user: discord.Member = None):
    await command_handlers.slash_payouts(interaction, action, user, process_seller_payout, process_all_payouts)

# ─────────────────────────────────────────────
#  ERROR HANDLER
# ─────────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    """Command error event handler"""
    await on_command_error_handler(ctx, error, prefix=PREFIX)

# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
