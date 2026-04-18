# tasks.py - Background tasks and scheduled operations
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from discord.ext import tasks
from shopbot.database import get_db, update_daily_sales_metrics, log_performance, create_database_backup, optimize_database, get_database_health
from utils import DB_FILE, INVOICE_REFRESH_INTERVAL, POLL_INTERVAL, PAYMENT_TIMEOUT, get_payment_poll_interval
from shopbot.crypto import get_address_balance, get_addresses_balance, litoshi_to_ltc, format_ltc
from src.services.payment_engine import process_automatic_refund
from src.services.order_manager import update_invoice_message, update_user_order_message, refresh_pending_invoice_messages
from src.services.stock_manager import update_stock_message, send_low_stock_alert, notify_next_in_queue
from ui.embeds import build_live_embed
from ui.views import ProductDetailView, AdminPanelView


@tasks.loop(seconds=INVOICE_REFRESH_INTERVAL)
async def refresh_invoice_timers(all_orders_callback, refresh_invoice_callback, refresh_order_callback):
    """Refresh invoice timers and order messages periodically"""
    pending_orders = [order for order in all_orders_callback() if order['status'] == 'pending']
    if not pending_orders:
        return

    for order in pending_orders:
        try:
            if order.get('invoice_message_id'):
                await refresh_invoice_callback(order)
            if order.get('message_id') and order.get('channel_id'):
                await refresh_order_callback(order)
        except Exception:
            pass


@tasks.loop(seconds=POLL_INTERVAL)
async def check_payments(
    all_orders_callback, get_address_balance_callback, get_addresses_balance_callback, litoshi_to_ltc_callback,
    update_invoice_callback, process_automatic_refund_callback, process_payment_delivery_callback,
    release_reserved_stock_callback, send_log_embed_callback, get_reserved_stock_items_for_order_callback,
    build_seller_payout_outputs_callback, bot_instance
):
    """Poll for payment confirmations and process orders with adaptive rate limiting"""
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status IN (?, ?, ?, ?, ?, ?) AND (swept_at IS NULL OR swept_at = '')", ('pending', 'paid', 'expired', 'canceled', 'failed', 'no_stock'))
    pending_orders = c.fetchall()
    conn.close()

    if not pending_orders:
        logging.debug("check_payments poll: no pending orders")
        return

    logging.info(f"check_payments poll: {len(pending_orders)} pending/paid/expired/canceled/failed/no_stock orders")

    # Adaptive polling: if we have many orders, be more conservative with API calls
    adaptive_delay = min(30, len(pending_orders) // 10)  # Add up to 30s delay for every 10 orders

    now = datetime.now(timezone.utc).timestamp()
    orders_with_payment = []
    checked_count = 0

    orders_to_check: list[dict] = []
    addresses_to_check: list[str] = []

    for order in pending_orders:
        order_dict = dict(order)
        oid = order_dict['id']

        if order_dict['status'] == 'canceled' and order_dict.get('refund_txid'):
            logging.debug(f"Skipping canceled order {oid[:8]} already refunded")
            continue

        last_check = order_dict.get('last_payment_check') or 0
        required_delay = get_payment_poll_interval(order_dict['created_at'], now) + adaptive_delay
        time_since_check = now - last_check

        if time_since_check < required_delay:
            logging.debug(f"Skipping order {oid[:8]} - checked {time_since_check:.1f}s ago (need {required_delay}s)")
            continue

        orders_to_check.append(order_dict)
        if order_dict.get('ltc_address'):
            addresses_to_check.append(order_dict['ltc_address'])

    if not orders_to_check:
        logging.debug("check_payments poll: no orders ready for payment check")
        return

    balance_results = await get_addresses_balance_callback(addresses_to_check)

    # First pass: detect payments and record detection time
    for order_dict in orders_to_check:
        oid = order_dict['id']
        address = order_dict.get('ltc_address')

        try:
            logging.debug(f"Checking payment for order {oid[:8]}...")
            balance_info = balance_results.get(address) if address else None
            if not balance_info:
                balance_info = await get_address_balance_callback(address) if address else None

            if not balance_info:
                logging.warning(f"Failed to get balance for order {oid[:8]}")
                continue

            checked_count += 1
            confirmed_balance = litoshi_to_ltc_callback(balance_info.get('balance', 0))
            unconfirmed_balance = litoshi_to_ltc_callback(balance_info.get('unconfirmed_balance', 0))

            # Update last check time
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE orders SET last_payment_check = ? WHERE id = ?", (now, oid))
            conn.commit()
            conn.close()

            expected_amount = Decimal(str(order_dict['price_ltc']))
            tolerance = Decimal('0.00000001') * 2

            if confirmed_balance >= expected_amount - tolerance:
                logging.info(f"✅ Payment detected for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC (expected: {format_ltc(expected_amount)} LTC)")
                orders_with_payment.append((order_dict, balance_info))

                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute(
                    "UPDATE orders SET status = 'paid', paid_at = ?, payment_detected_at = ? WHERE id = ?",
                    (now, now, oid)
                )
                conn.commit()
                conn.close()

                await update_invoice_callback(order_dict, balance_info)

                from shopbot.database import get_product
                product = get_product(order_dict['product_id'])
                if product:
                    await send_log_embed_callback(
                        "💰 Payment Detected",
                        f"Order {oid[:8]} - {product['name']}\nAmount: {format_ltc(expected_amount)} LTC",
                        {"Order ID": oid[:8], "User": f"<@{order_dict['user_id']}>", "Product": product['name']}
                    )

            elif order_dict['status'] == 'pending' and order_dict['created_at'] + PAYMENT_TIMEOUT < now:
                logging.info(f"⏰ Order {oid[:8]} expired (no payment within {PAYMENT_TIMEOUT}s)")
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (oid,))
                conn.commit()
                conn.close()

                await update_invoice_callback(order_dict, balance_info)
                release_reserved_stock_callback(oid)
                await notify_next_in_queue(order_dict['product_id'])

            elif order_dict['status'] == 'canceled' and (confirmed_balance > 0 or unconfirmed_balance > 0):
                await process_automatic_refund_callback(order_dict, balance_info, bot_instance)

        except Exception as e:
            logging.error(f"Error checking payment for order {oid}: {e}")

    logging.info(f"Payment poll summary: checked {checked_count}/{len(pending_orders)} orders, {len(orders_with_payment)} payments detected")

    # Second pass: process deliveries for paid orders
    for order_dict, balance_info in orders_with_payment:
        try:
            await process_payment_delivery_callback(
                order_dict, balance_info, bot_instance,
                update_invoice_callback, update_user_order_message, update_stock_message,
                notify_next_in_queue, send_log_embed_callback, get_reserved_stock_items_for_order_callback,
                build_seller_payout_outputs_callback
            )
        except Exception as e:
            logging.error(f"Error processing delivery for order {order_dict['id']}: {e}")


async def handle_blockcypher_webhook_event(
    address: str,
    bot_instance,
    update_invoice_callback,
    process_automatic_refund_callback,
    process_payment_delivery_callback,
    release_reserved_stock_callback,
    send_log_embed_callback,
    get_reserved_stock_items_for_order_callback,
    build_seller_payout_outputs_callback,
) -> bool:
    """Handle incoming BlockCypher webhook events for an address."""
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM orders WHERE ltc_address = ? AND status IN (?, ?, ?) AND (swept_at IS NULL OR swept_at = '')",
        (address, 'pending', 'paid', 'canceled')
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        logging.info(f"Webhook event for {address} did not match any active orders")
        return False

    balance_info = await get_address_balance(address)
    if not balance_info:
        logging.warning(f"Webhook event for {address} could not fetch balance")
        return False

    confirmed_balance = litoshi_to_ltc_callback(balance_info.get('balance', 0))
    unconfirmed_balance = litoshi_to_ltc_callback(balance_info.get('unconfirmed_balance', 0))
    now = datetime.now(timezone.utc).timestamp()
    processed = False
    delivery_candidates: list[tuple[dict, dict]] = []

    for row in rows:
        order_dict = dict(row)
        oid = order_dict['id']
        expected_amount = Decimal(str(order_dict['price_ltc']))
        tolerance = Decimal('0.00000001') * 2

        if order_dict['status'] == 'pending' and confirmed_balance >= expected_amount - tolerance:
            logging.info(f"Webhook payment detected for order {oid[:8]} at {address}")
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute(
                "UPDATE orders SET status = 'paid', paid_at = ?, payment_detected_at = ? WHERE id = ?",
                (now, now, oid)
            )
            conn.commit()
            conn.close()
            await update_invoice_callback(order_dict, balance_info)
            delivery_candidates.append((order_dict, balance_info))
            processed = True

        elif order_dict['status'] == 'paid':
            delivery_candidates.append((order_dict, balance_info))
            processed = True

        elif order_dict['status'] == 'canceled' and (confirmed_balance > 0 or unconfirmed_balance > 0):
            logging.info(f"Webhook refund triggered for canceled order {oid[:8]} at {address}")
            await process_automatic_refund_callback(order_dict, balance_info, bot_instance)
            processed = True

    for order_dict, balance_info in delivery_candidates:
        try:
            await process_payment_delivery_callback(
                order_dict, balance_info, bot_instance,
                update_invoice_callback, update_user_order_message, update_stock_message,
                notify_next_in_queue, send_log_embed_callback, get_reserved_stock_items_for_order_callback,
                build_seller_payout_outputs_callback
            )
        except Exception as e:
            logging.error(f"Error processing delivery for order {order_dict['id']} from webhook: {e}")

    return processed


@tasks.loop(hours=1)  # Update analytics every hour
async def update_analytics():
    """Update sales analytics and performance metrics"""
    try:
        import time

        start_time = time.time()
        update_daily_sales_metrics(DB_FILE)
        duration = (time.time() - start_time) * 1000

        log_performance("analytics_update", duration, True)
        logging.debug(f"📊 Analytics updated in {duration:.1f}ms")

    except Exception as e:
        logging.error(f"❌ Analytics update failed: {e}")


@tasks.loop(hours=24)  # Daily backup
async def database_backup():
    """Create daily database backup"""
    try:
        import time

        start_time = time.time()
        backup_file = create_database_backup(DB_FILE)
        duration = (time.time() - start_time) * 1000

        if backup_file:
            logging.info(f"💾 Database backup completed in {duration:.1f}ms: {backup_file}")
        else:
            logging.error("❌ Database backup failed")

    except Exception as e:
        logging.error(f"❌ Database backup task failed: {e}")


@tasks.loop(hours=168)  # Weekly maintenance (7 days)
async def database_maintenance():
    """Perform weekly database maintenance"""
    try:
        import time

        start_time = time.time()

        # Get health metrics before optimization
        health_before = get_database_health(DB_FILE)

        # Optimize database
        if optimize_database(DB_FILE):
            # Get health metrics after optimization
            health_after = get_database_health(DB_FILE)

            duration = (time.time() - start_time) * 1000
            logging.info(f"🔧 Database maintenance completed in {duration:.1f}ms")
            logging.info(f"   Size: {health_after.get('database_size_mb', 0)}MB")

            # Log performance improvement
            log_performance("database_maintenance", duration, True)

        else:
            logging.error("❌ Database optimization failed")

    except Exception as e:
        logging.error(f"❌ Database maintenance failed: {e}")