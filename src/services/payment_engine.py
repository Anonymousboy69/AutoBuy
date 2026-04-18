# payment_engine.py - Payment processing and cryptocurrency operations
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple
from discord.ext import tasks

from shopbot.database import get_db, get_order, all_orders, get_product, assign_order_stock_to_order, log_audit
from shopbot.crypto import sweep_payment, get_address_balance, litoshi_to_ltc, format_ltc, find_address_path_by_address
from utils import (
    DB_FILE, LTC_CONFIRMATIONS, MAX_SWEEP_ATTEMPTS, SWEEP_RETRY_BACKOFF,
    MAX_REFUND_ATTEMPTS, RECEIVING_ADDRESS, COLORS, LOGGING_CHANNEL_ID,
    PAYMENT_TIMEOUT, RESERVATION_TIMEOUT, POLL_INTERVAL, WALLET_SEED,
    get_next_blockcypher_token
)

async def get_address_transactions(address: str) -> list:
    """Fetch all incoming transactions for an address from BlockCypher"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}?txn=true"
            url += f"&token={get_next_blockcypher_token()}"

            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    txns = data.get('txrefs', [])
                    return txns
    except Exception as e:
        logging.warning(f"Could not fetch transactions for {address}: {e}")
    return []


async def process_automatic_refund(order_dict, balance_info, bot_instance):
    """Automatically refund payments detected on canceled orders"""
    oid = order_dict['id']
    user_id = order_dict['user_id']
    product_id = order_dict['product_id']

    refund_attempts = order_dict.get('refund_attempts', 0)
    if refund_attempts >= MAX_REFUND_ATTEMPTS:
        logging.warning(f"Order {oid[:8]} has reached max refund attempts ({MAX_REFUND_ATTEMPTS})")
        return

    confirmed = litoshi_to_ltc(balance_info.get("balance", 0))
    unconfirmed = litoshi_to_ltc(balance_info.get("unconfirmed_balance", 0))
    if confirmed > 0:
        refund_amount = confirmed
    elif unconfirmed > 0:
        logging.warning(f"Refund for order {oid[:8]} is pending confirmation and will be retried later")
        return
    else:
        logging.warning(f"No funds available to refund for order {oid[:8]}")
        return

    logging.info(f"Processing automatic refund for canceled order {oid[:8]}: {format_ltc(refund_amount)} LTC")

    address_path = order_dict.get("address_path")
    if not address_path:
        address_path = find_address_path_by_address(DB_FILE, order_dict['ltc_address'], WALLET_SEED)
        if address_path:
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('UPDATE orders SET address_path = ? WHERE id = ?', (address_path, oid))
            conn.commit()
            conn.close()
    if not address_path:
        logging.error(f"No address path for refund of order {oid[:8]}")
        return

    refund_txid = None
    sweep_success = False

    try:
        sweep_success, refund_txid = await sweep_payment(
            DB_FILE,
            address_path,
            order_dict['ltc_address'],
            refund_amount,
            WALLET_SEED,
            RECEIVING_ADDRESS,
            get_next_blockcypher_token(),
            LTC_CONFIRMATIONS,
            recipients=None,
        )
    except Exception as e:
        logging.error(f"Refund sweep failed for order {oid[:8]}: {e}")

    # Update order with refund info
    now = datetime.now(timezone.utc).timestamp()
    conn = get_db(DB_FILE)
    c = conn.cursor()

    if sweep_success:
        c.execute(
            "UPDATE orders SET status = 'refunded', refund_txid = ?, refund_attempts = refund_attempts + 1 WHERE id = ?",
            (refund_txid, oid)
        )
        logging.info(f"✅ Refund successful for order {oid[:8]}: {refund_txid}")
    else:
        c.execute(
            "UPDATE orders SET refund_attempts = refund_attempts + 1, last_refund_attempt = ? WHERE id = ?",
            (now, oid)
        )
        logging.warning(f"❌ Refund failed for order {oid[:8]} (attempt {refund_attempts + 1}/{MAX_REFUND_ATTEMPTS})")

    conn.commit()
    conn.close()

    # Notify user
    try:
        import discord
        user = bot_instance.get_user(int(user_id)) or await bot_instance.fetch_user(int(user_id))
        if user:
            em = discord.Embed(
                title="💸 Refund Processed" if sweep_success else "⚠️ Refund Failed",
                description=f"Your canceled order {oid[:8]} has been refunded." if sweep_success else f"Refund failed for your canceled order {oid[:8]}. Please contact support.",
                color=COLORS["success"] if sweep_success else COLORS["error"]
            )
            em.add_field(name="Order ID", value=f"`{oid[:8]}`", inline=True)
            em.add_field(name="Refund Amount", value=f"{format_ltc(refund_amount)} LTC", inline=True)
            if sweep_success and refund_txid:
                em.add_field(name="Transaction ID", value=f"`{refund_txid[:16]}...`", inline=False)
            await user.send(embed=em)
    except Exception as e:
        logging.warning(f"Could not notify user {user_id} about refund for order {oid[:8]}: {e}")


async def process_payment_delivery(order_dict, balance_info, bot_instance, update_invoice_callback, update_user_order_callback, update_stock_callback, notify_next_callback, send_log_callback, get_reserved_stock_callback, build_seller_payout_callback):
    """Process delivery for a paid order"""
    oid = order_dict['id']
    user_id = order_dict['user_id']
    product_id = order_dict['product_id']
    quantity = int(order_dict.get('quantity', 1))

    logging.info(f"🚚 Processing delivery for order {oid[:8]} (quantity: {quantity})")

    # Assign stock items to order
    success = assign_order_stock_to_order(order_dict)
    if not success:
        logging.error(f"Failed to assign stock for order {oid[:8]}")
        # Update order status to failed
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE orders SET status = 'failed' WHERE id = ?", (oid,))
        conn.commit()
        conn.close()

        # Notify user
        try:
            import discord
            user = bot_instance.get_user(int(user_id)) or await bot_instance.fetch_user(int(user_id))
            if user:
                product = get_product(product_id)
                em = discord.Embed(
                    title="❌ Delivery Failed",
                    description="We could not complete your order due to stock allocation issues. Please contact support.",
                    color=COLORS["error"]
                )
                em.add_field(name="Order ID", value=f"`{oid[:8]}`", inline=True)
                if product:
                    em.add_field(name="Product", value=product['name'], inline=True)
                await user.send(embed=em)
        except Exception as e:
            logging.warning(f"Could not notify user {user_id} about delivery failure for order {oid[:8]}: {e}")

        await update_invoice_callback(order_dict, balance_info)
        return

    # Attempt to sweep payment
    sweep_attempts = order_dict.get('sweep_attempts', 0)
    max_attempts = MAX_SWEEP_ATTEMPTS

    if sweep_attempts < max_attempts:
        address_path = order_dict.get('address_path')
        if not address_path:
            address_path = find_address_path_by_address(DB_FILE, order_dict['ltc_address'], WALLET_SEED)
            if address_path:
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE orders SET address_path = ? WHERE id = ?', (address_path, oid))
                conn.commit()
                conn.close()

        if address_path:
            try:
                recipients = None
                if order_dict.get('status') == 'paid':
                    # Build seller payout outputs for multi-seller orders
                    recipients_result = build_seller_payout_callback(order_dict)
                    if recipients_result[0]:
                        recipients = recipients_result[0]

                sweep_success, sweep_txid = await sweep_payment(
                    DB_FILE,
                    address_path,
                    order_dict['ltc_address'],
                    Decimal(str(order_dict['price_ltc'])),
                    WALLET_SEED,
                    RECEIVING_ADDRESS,
                    get_next_blockcypher_token(),
                    LTC_CONFIRMATIONS,
                    recipients=recipients,
                )

                now = datetime.now(timezone.utc).timestamp()
                conn = get_db(DB_FILE)
                c = conn.cursor()

                if sweep_success:
                    c.execute(
                        "UPDATE orders SET swept_at = ?, sweep_txid = ?, sweep_attempts = ? WHERE id = ?",
                        (now, sweep_txid, sweep_attempts + 1, oid)
                    )
                    logging.info(f"✅ Payment swept for order {oid[:8]}: {sweep_txid}")
                else:
                    c.execute(
                        "UPDATE orders SET sweep_attempts = ?, last_sweep_attempt = ? WHERE id = ?",
                        (sweep_attempts + 1, now, oid)
                    )
                    logging.warning(f"❌ Sweep failed for order {oid[:8]} (attempt {sweep_attempts + 1}/{max_attempts})")

                conn.commit()
                conn.close()

                if not sweep_success:
                    logging.warning(f"Order {oid[:8]} will remain in paid state until sweep succeeds")
                    return

            except Exception as e:
                logging.error(f"Sweep error for order {oid[:8]}: {e}")
                return
        else:
            logging.warning(f"No address path for order {oid[:8]}, skipping sweep")
            return

    # Update order status to delivered
    now = datetime.now(timezone.utc).timestamp()
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE orders SET status = 'delivered', delivered_at = ? WHERE id = ?", (now, oid))
    conn.commit()
    conn.close()

    # Update invoice message
    await update_invoice_callback(order_dict, balance_info)

    # Update user's order message
    await update_user_order_callback(order_dict)

    # Update stock message
    await update_stock_callback(product_id)

    # Send delivery DM with items
    try:
        import discord
        user = bot_instance.get_user(int(user_id)) or await bot_instance.fetch_user(int(user_id))
        if user:
            product = get_product(product_id)

            # Get delivered items
            delivered_items = get_reserved_stock_callback(oid)

            em = discord.Embed(
                title="✅ Order Delivered!",
                description=f"Your order has been completed successfully!",
                color=COLORS["success"]
            )
            em.add_field(name="Order ID", value=f"`{oid[:8]}`", inline=True)
            if product:
                em.add_field(name="Product", value=product['name'], inline=True)
            em.add_field(name="Quantity", value=str(quantity), inline=True)
            em.add_field(name="Total Paid", value=f"{format_ltc(Decimal(str(order_dict['price_ltc'])))} LTC", inline=True)

            await user.send(embed=em)

            # Send each item as a separate message with file attachment
            for i, item in enumerate(delivered_items, 1):
                content = item['content']
                filename = f"order_{oid[:8]}_item_{i}.txt"

                # Create file attachment
                from io import StringIO
                file = discord.File(StringIO(content), filename=filename)

                item_em = discord.Embed(
                    title=f"📦 Item {i} of {len(delivered_items)}",
                    description=f"Order: `{oid[:8]}`",
                    color=COLORS["info"]
                )

                await user.send(embed=item_em, file=file)

    except Exception as e:
        logging.error(f"Failed to deliver items for order {oid[:8]}: {e}")

    # Log delivery
    log_audit(product_id, 'order_delivered', 'system', 'system', quantity, f"Order {oid[:8]} delivered to user {user_id}")

    # Send log embed
    product = get_product(product_id)
    if product:
        await send_log_callback(
            "✅ Order Delivered",
            f"Order {oid[:8]} - {product['name']} (x{quantity})",
            {"Order ID": oid[:8], "User": f"<@{user_id}>", "Product": product['name'], "Quantity": str(quantity)}
        )

    # Notify next in queue
    await notify_next_callback(product_id)