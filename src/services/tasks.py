# tasks.py - Background tasks and scheduled operations
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from discord.ext import tasks
from shopbot.database import get_db, update_daily_sales_metrics, log_performance, create_database_backup, optimize_database, get_database_health
from utils import DB_FILE, INVOICE_REFRESH_INTERVAL, POLL_INTERVAL, PAYMENT_TIMEOUT, get_payment_poll_interval, get_next_blockcypher_token
from shopbot.crypto import get_address_balance, get_addresses_balance, litoshi_to_ltc, format_ltc, delete_blockcypher_webhook, validate_transaction_safety, check_transaction_uniqueness, validate_address_ownership
from src.services.payment_engine import process_automatic_refund
from src.services.order_manager import update_invoice_message, update_user_order_message, refresh_pending_invoice_messages
from src.services.stock_manager import update_stock_message, send_low_stock_alert, notify_next_in_queue
from ui.embeds import build_live_embed
from ui.views import ProductDetailView, AdminPanelView


async def retry_api_call(func, *args, max_retries=3, delay=1.0, **kwargs):
    """Retry an API call with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            wait_time = delay * (2 ** attempt)
            logging.warning(f"API call failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)


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
    send_log_embed_callback, build_seller_payout_outputs_callback, notify_next_callback, bot_instance
):
    """Poll for payment confirmations and process orders with adaptive rate limiting"""
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status = ? AND (swept_at IS NULL OR swept_at = '')", ('pending',))
    pending_orders = c.fetchall()
    conn.close()

    if not pending_orders:
        logging.debug("check_payments poll: no pending orders")
        return

    logging.info(f"check_payments poll: {len(pending_orders)} pending orders")

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
                balance_info = await retry_api_call(get_address_balance_callback, address) if address else None

            if not balance_info:
                logging.warning(f"Failed to get balance for order {oid[:8]} after retries")
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
                # TRANSACTION SAFETY VALIDATION - Critical for real money
                address = order_dict.get('ltc_address')
                if not address:
                    logging.error(f"No LTC address for order {oid[:8]}, skipping payment detection")
                    continue

                # Validate address ownership (ensure it belongs to our wallet)
                from utils import WALLET_SEED
                if WALLET_SEED:
                    address_valid = await validate_address_ownership(address, WALLET_SEED)
                    if not address_valid:
                        logging.error(f"Address {address} does not belong to our wallet for order {oid[:8]}")
                        continue

                # Store transaction details for later use
                payment_txid = None
                payment_confirmations = 0

                # Get transaction details to validate safety
                try:
                    # Get recent transactions for this address
                    from shopbot.crypto import get_address_transactions
                    transactions = await get_address_transactions(address)

                    # Find the transaction that brought the balance to the expected amount
                    payment_tx = None
                    for tx in transactions:
                        if tx.get('value', 0) > 0:  # Incoming transaction
                            tx_value_ltc = litoshi_to_ltc_callback(tx.get('value', 0))
                            if abs(tx_value_ltc - expected_amount) <= tolerance:
                                payment_tx = tx
                                break

                    if payment_tx:
                        txid = payment_tx.get('tx_hash')
                        if txid:
                            payment_txid = txid
                            # Validate transaction safety
                            validation_result = await validate_transaction_safety(
                                txid=txid,
                                expected_address=address,
                                expected_amount_ltc=expected_amount,
                                blockcypher_token=get_next_blockcypher_token(),
                                min_confirmations=1  # Require at least 1 confirmation
                            )

                            if not validation_result['valid']:
                                logging.warning(f"Transaction validation failed for order {oid[:8]}: {', '.join(validation_result['errors'])}")
                                if validation_result['warnings']:
                                    logging.warning(f"Transaction warnings for order {oid[:8]}: {', '.join(validation_result['warnings'])}")
                                continue

                            payment_confirmations = validation_result['confirmations']

                            # Check transaction uniqueness (prevent double-processing)
                            tx_unique = await check_transaction_uniqueness(txid, DB_FILE)
                            if not tx_unique:
                                logging.warning(f"Transaction {txid} already processed, skipping order {oid[:8]}")
                                continue

                            logging.info(f"✅ Transaction validation passed for order {oid[:8]}: {txid} ({validation_result['confirmations']} confirmations)")

                except Exception as e:
                    logging.error(f"Transaction validation error for order {oid[:8]}: {e}")
                    continue

                # Validate payment amount is not excessively over the expected amount
                overpayment_threshold = expected_amount * Decimal('1.1')  # 10% overpayment threshold
                if confirmed_balance > overpayment_threshold:
                    logging.warning(f"⚠️ Overpayment detected for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC (expected: {format_ltc(expected_amount)} LTC)")
                    # Still process but log the overpayment
                elif confirmed_balance < expected_amount - tolerance:
                    logging.debug(f"Payment incomplete for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC (expected: {format_ltc(expected_amount)} LTC)")
                    continue

                logging.info(f"✅ Payment detected and validated for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC (expected: {format_ltc(expected_amount)} LTC)")
                orders_with_payment.append((order_dict, balance_info))
                if order_dict.get('blockcypher_hook_id'):
                    try:
                        deleted = await delete_blockcypher_webhook(order_dict['blockcypher_hook_id'], get_next_blockcypher_token())
                        if deleted:
                            cleanup_conn = get_db(DB_FILE)
                            cleanup_cursor = cleanup_conn.cursor()
                            cleanup_cursor.execute("UPDATE orders SET blockcypher_hook_id = NULL WHERE id = ?", (oid,))
                            cleanup_conn.commit()
                            cleanup_conn.close()
                    except Exception as e:
                        logging.warning(f"Failed to delete BlockCypher webhook for order {oid[:8]}: {e}")
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute(
                    "UPDATE orders SET status = 'paid', paid_at = ?, payment_detected_at = ?, payment_txid = ?, payment_confirmations = ? WHERE id = ?",
                    (now, now, payment_txid, payment_confirmations, oid)
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

                if order_dict.get('blockcypher_hook_id'):
                    try:
                        deleted = await delete_blockcypher_webhook(order_dict['blockcypher_hook_id'], get_next_blockcypher_token())
                        if deleted:
                            cleanup_conn = get_db(DB_FILE)
                            cleanup_cursor = cleanup_conn.cursor()
                            cleanup_cursor.execute("UPDATE orders SET blockcypher_hook_id = NULL WHERE id = ?", (oid,))
                            cleanup_conn.commit()
                            cleanup_conn.close()
                    except Exception as e:
                        logging.warning(f"Failed to delete BlockCypher webhook for expired order {oid[:8]}: {e}")

                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (oid,))
                conn.commit()
                conn.close()

                await update_invoice_callback(order_dict, balance_info)
                await notify_next_callback(order_dict['product_id'])

            elif order_dict['status'] == 'canceled' and (confirmed_balance > 0 or unconfirmed_balance > 0):
                await process_automatic_refund_callback(order_dict, balance_info, bot_instance)

            elif order_dict['status'] == 'pending' and (confirmed_balance > 0 or unconfirmed_balance > 0):
                # PARTIAL PAYMENT DETECTED - Update invoice to show balance even if not full payment
                logging.info(f"⚠️ Partial payment detected for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC confirmed, {format_ltc(unconfirmed_balance)} LTC unconfirmed (expected: {format_ltc(Decimal(str(order_dict['price_ltc'])))} LTC)")
                await update_invoice_callback(order_dict, balance_info)

        except Exception as e:
            logging.error(f"Error checking payment for order {oid}: {e}")
            # Continue processing other orders even if one fails
            continue

    logging.info(f"Payment poll summary: checked {checked_count}/{len(pending_orders)} orders, {len(orders_with_payment)} payments detected")

    # Second pass: process deliveries for paid orders
    for order_dict, balance_info in orders_with_payment:
        try:
            await process_payment_delivery_callback(
                order_dict, balance_info, bot_instance,
                update_invoice_callback, update_user_order_message, update_stock_message,
                notify_next_in_queue, send_log_embed_callback,
                build_seller_payout_outputs_callback
            )
        except Exception as e:
            oid = order_dict['id']
            logging.error(f"Error processing delivery for order {oid}: {e}")
            # Mark order as failed if delivery processing fails
            try:
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE orders SET status = 'failed', error_message = ? WHERE id = ?", (str(e), oid))
                conn.commit()
                conn.close()
                logging.warning(f"Marked order {oid[:8]} as failed due to delivery error")
            except Exception as db_e:
                logging.error(f"Failed to update order status for {oid}: {db_e}")


async def handle_blockcypher_webhook_event(
    order_id: str,
    address: str,
    bot_instance,
    update_invoice_callback,
    refresh_order_message_callback,
    process_automatic_refund_callback,
    process_payment_delivery_callback,
    send_log_embed_callback,
    build_seller_payout_outputs_callback,
) -> bool:
    """Handle incoming BlockCypher webhook events for a single order."""
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM orders WHERE id = ? AND status IN (?, ?, ?) AND (swept_at IS NULL OR swept_at = '')",
        (order_id, 'pending', 'paid', 'canceled')
    )
    row = c.fetchone()
    conn.close()

    if not row:
        logging.info(f"Webhook event for order {order_id} did not match any active orders")
        return False

    order_dict = dict(row)
    if address and order_dict.get('ltc_address') != address:
        logging.warning(
            f"Webhook order {order_id} address mismatch: expected {order_dict.get('ltc_address')} got {address}"
        )

    balance_info = await get_address_balance(order_dict.get('ltc_address'))
    if not balance_info:
        logging.warning(f"Webhook event for order {order_id} could not fetch balance")
        return False

    confirmed_balance = litoshi_to_ltc(balance_info.get('balance', 0))
    unconfirmed_balance = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0))
    now = datetime.now(timezone.utc).timestamp()
    processed = False
    delivery_candidates: list[tuple[dict, dict]] = []
    oid = order_dict['id']
    expected_amount = Decimal(str(order_dict['price_ltc']))
    tolerance = Decimal('0.00000001') * 2

    async def _cleanup_hook() -> None:
        if not order_dict.get('blockcypher_hook_id'):
            return
        try:
            deleted = await delete_blockcypher_webhook(order_dict['blockcypher_hook_id'], get_next_blockcypher_token())
            if deleted:
                cleanup_conn = get_db(DB_FILE)
                cleanup_cursor = cleanup_conn.cursor()
                cleanup_cursor.execute("UPDATE orders SET blockcypher_hook_id = NULL WHERE id = ?", (oid,))
                cleanup_conn.commit()
                cleanup_conn.close()
        except Exception as e:
            logging.warning(f"Failed to delete BlockCypher webhook for order {oid[:8]}: {e}")

    if order_dict['status'] == 'pending' and confirmed_balance >= expected_amount - tolerance:
        logging.info(f"Webhook payment detected for order {oid[:8]} at {address}")
        await _cleanup_hook()
        
        # Extract transaction ID and confirmations (same as polling handler)
        payment_txid = None
        payment_confirmations = 0
        try:
            from shopbot.crypto import get_address_transactions
            transactions = await get_address_transactions(address)
            
            # Find the transaction that brought the balance to the expected amount
            payment_tx = None
            for tx in transactions:
                if tx.get('value', 0) > 0:  # Incoming transaction
                    tx_value_ltc = litoshi_to_ltc(tx.get('value', 0))
                    if abs(tx_value_ltc - expected_amount) <= tolerance:
                        payment_tx = tx
                        break
            
            if payment_tx:
                payment_txid = payment_tx.get('tx_hash')
                payment_confirmations = payment_tx.get('confirmations', 0)
                logging.info(f"Captured transaction for order {oid[:8]}: {payment_txid} ({payment_confirmations} confirmations)")
        except Exception as e:
            logging.warning(f"Failed to extract transaction details for webhook order {oid[:8]}: {e}")
        
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute(
            "UPDATE orders SET status = 'paid', paid_at = ?, payment_detected_at = ?, payment_txid = ?, payment_confirmations = ? WHERE id = ?",
            (now, now, payment_txid, payment_confirmations, oid)
        )
        conn.commit()
        conn.close()
        order_dict['status'] = 'paid'
        if payment_txid:
            order_dict['payment_txid'] = payment_txid
        await update_invoice_callback(order_dict, balance_info)
        delivery_candidates.append((order_dict, balance_info))
        processed = True

    elif order_dict['status'] == 'paid':
        await _cleanup_hook()
        delivery_candidates.append((order_dict, balance_info))
        processed = True

    elif order_dict['status'] == 'canceled' and (confirmed_balance > 0 or unconfirmed_balance > 0):
        await _cleanup_hook()
        logging.info(f"Webhook refund triggered for canceled order {oid[:8]} at {address}")
        await process_automatic_refund_callback(order_dict, balance_info, bot_instance)
        processed = True

    elif order_dict['status'] == 'pending' and (confirmed_balance > 0 or unconfirmed_balance > 0):
        # PARTIAL PAYMENT - Update invoice to show balance even if not full amount
        await _cleanup_hook()
        logging.info(f"⚠️ Webhook detected partial payment for order {oid[:8]}: {format_ltc(confirmed_balance)} LTC confirmed (expected: {format_ltc(expected_amount)} LTC)")
        await update_invoice_callback(order_dict, balance_info)
        # Also refresh the user's order message to show updated payment status
        if refresh_order_message_callback:
            await refresh_order_message_callback(order_dict)
        processed = True

    for order_dict, balance_info in delivery_candidates:
        try:
            await process_payment_delivery_callback(
                order_dict, balance_info, bot_instance,
                update_invoice_callback, update_user_order_message, update_stock_message,
                notify_next_in_queue, send_log_embed_callback,
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