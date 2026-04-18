# order_manager.py - Order and invoice message management
import logging
import urllib.parse
from decimal import Decimal
from datetime import datetime, timezone

from shopbot.database import get_db
from utils import INVOICE_CHANNEL_ID, COLORS, get_order_expiration_footer, PAYMENT_TIMEOUT
from ui.embeds import build_invoice_embed
from ui.views import InvoiceApproveView, OrderCancelView


async def update_invoice_message(order: dict, balance_info: dict | None, get_channel_callback, get_product_callback, get_address_transactions_callback, litoshi_to_ltc_callback):
    """Update the invoice message for an order"""
    channel_id = order.get('invoice_channel_id') or INVOICE_CHANNEL_ID
    message_id = order.get('invoice_message_id')
    if not channel_id or not message_id:
        logging.debug(f"Skipping invoice update for order {order['id']}: channel_id={channel_id}, message_id={message_id}")
        return

    try:
        channel = await get_channel_callback(channel_id)
        if channel is None:
            logging.warning(f"Invoice channel {channel_id} not available for order {order['id']}")
            return
        msg = await channel.fetch_message(int(message_id))
        product = get_product_callback(order['product_id'])
        if not product:
            return

        # Fetch transactions - but don't fail if this fails
        transactions = None
        try:
            transactions = await get_address_transactions_callback(order['ltc_address'])
        except Exception as tx_err:
            logging.debug(f"Could not fetch transactions for order {order['id']}: {tx_err}")

        disabled = order['status'] in {'delivered', 'expired', 'failed', 'sweep_failed', 'canceled', 'refunded'}

        # Show refund button if there's balance on the address (for manual refunds)
        show_refund = False
        show_sweep = False
        if balance_info:
            confirmed = litoshi_to_ltc_callback(balance_info.get('balance', 0))
            unconfirmed = litoshi_to_ltc_callback(balance_info.get('unconfirmed_balance', 0))
            if (confirmed > 0 or unconfirmed > 0) and order['status'] in {'canceled', 'pending', 'expired'}:
                show_refund = True
            if confirmed > 0 and order['status'] == 'pending':
                show_sweep = True

        view = InvoiceApproveView(order['id'], disabled=disabled, show_refund=show_refund, show_sweep=show_sweep)
        await msg.edit(embed=build_invoice_embed(order, product, balance_info, transactions=transactions), view=view)
        logging.info(f"Updated invoice message for order {order['id']}, status: {order['status']}")
    except Exception as e:
        logging.error(f"Could not update invoice message for order {order['id']}: {e}", exc_info=True)


async def refresh_invoice_message(order: dict, get_channel_callback, get_product_callback):
    """Refresh the invoice message without balance info"""
    channel_id = order.get('invoice_channel_id') or INVOICE_CHANNEL_ID
    message_id = order.get('invoice_message_id')
    if not channel_id or not message_id:
        return

    try:
        channel = await get_channel_callback(channel_id)
        if channel is None:
            return

        msg = await channel.fetch_message(int(message_id))
        product = get_product_callback(order['product_id'])
        if not product:
            return

        disabled = order['status'] in {'delivered', 'expired', 'failed', 'sweep_failed', 'canceled', 'refunded'}
        view = InvoiceApproveView(order['id'], disabled=disabled)
        await msg.edit(embed=build_invoice_embed(order, product, None), view=view)
    except Exception as e:
        logging.debug(f"Could not refresh invoice message for order {order['id']}: {e}")


def build_order_embed(order: dict, product: dict, format_ltc_callback) -> 'discord.Embed':
    """Build an embed for order display"""
    import discord
    total_price_ltc = Decimal(str(order.get('price_ltc', '0')))
    quantity = Decimal(str(order.get('quantity', 1)))
    unit_price_ltc = (total_price_ltc / quantity).quantize(Decimal('0.00000001'), rounding='ROUND_DOWN') if quantity else total_price_ltc
    price_usd = product.get('price_usd') if product else None
    title = "Order Created"
    color = COLORS["info"]
    if order.get('status') == 'canceled':
        title = f"❌ Order Canceled — {order['id'][:8]}"
        color = COLORS.get('error', COLORS["warning"])
    em = discord.Embed(title=title, color=color)
    em.add_field(name="Product", value=product["name"], inline=True)
    em.add_field(name="Quantity", value=f"×{order.get('quantity', 1)}", inline=True)
    em.add_field(name="Unit Price", value=f"{format_ltc_callback(unit_price_ltc)} LTC", inline=True)
    em.add_field(name="Total Amount", value=f"{format_ltc_callback(total_price_ltc)} LTC", inline=True)
    if price_usd is not None:
        total_usd = float(price_usd) * float(quantity)
        em.add_field(name="Total USD Price", value=f"${total_usd:.2f}", inline=True)
    em.add_field(name="Order ID", value=f"`{order['id'][:8]}`", inline=True)
    em.add_field(name="Blockchain", value="Litecoin", inline=True)
    em.add_field(name="Status", value=order.get('status', 'pending').capitalize(), inline=True)
    em.add_field(name="Payment Address", value=f"```{order['ltc_address']}```", inline=False)
    if order.get('status') == 'canceled':
        em.add_field(
            name="Note",
            value="This order was canceled before payment. No blockchain payment is expected.",
            inline=False,
        )
    else:
        em.add_field(
            name="Important",
            value="Do not click Cancel Order if you have already paid. Once payment is detected, canceling is disabled.",
            inline=False,
        )
    em.set_footer(text=get_order_expiration_footer(order['created_at'], PAYMENT_TIMEOUT))
    return em


async def update_user_order_message(order: dict, get_channel_callback, get_product_callback, bot_instance, format_ltc_callback):
    """Update the user's DM message when order is delivered."""
    channel_id = order.get('channel_id')
    message_id = order.get('message_id')

    if not channel_id or not message_id:
        logging.debug(f"No user DM message to update for order {order['id']}")
        return

    try:
        channel = await get_channel_callback(channel_id)
        if channel is None and order.get('user_id'):
            try:
                user_id_int = int(order['user_id'])
                user = bot_instance.get_user(user_id_int) or await bot_instance.fetch_user(user_id_int)
                if user:
                    channel = user.dm_channel or await user.create_dm()
            except Exception as e:
                logging.debug(f"Could not resolve DM channel for user order {order['id']} user {order.get('user_id')}: {e}")

        if channel is None:
            logging.warning(f"User DM channel {channel_id} not available for order {order['id']}")
            return

        msg = await channel.fetch_message(int(message_id))
        product = get_product_callback(order['product_id'])
        if not product:
            return

        # Build updated embed for delivered order
        quantity = int(order.get('quantity', 1))
        total_ltc = Decimal(str(order.get('price_ltc', '0')))
        total_usd = float(product.get('price_usd', 0.0)) * quantity if product.get('price_usd') is not None else None
        created_at = None
        paid_at = None
        delivered_at = None

        if order.get('created_at'):
            created_at = datetime.fromtimestamp(order['created_at'], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if order.get('paid_at'):
            paid_at = datetime.fromtimestamp(order['paid_at'], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if order.get('delivered_at'):
            delivered_at = datetime.fromtimestamp(order['delivered_at'], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        import discord
        em = discord.Embed(
            title="✅ Order Delivered",
            description="Your purchase has been completed and delivered successfully. Thank you for shopping with us!",
            color=COLORS["success"]
        )
        em.add_field(name="Product", value=product["name"], inline=True)
        em.add_field(name="Quantity", value=f"×{quantity}", inline=True)
        em.add_field(name="Order ID", value=f"`{order['id'][:8]}`", inline=True)
        em.add_field(name="Status", value="✅ Delivered", inline=True)
        em.add_field(name="Total Amount", value=f"{format_ltc_callback(total_ltc)} LTC", inline=True)
        if total_usd is not None:
            em.add_field(name="Total USD", value=f"${total_usd:.2f}", inline=True)

        if order.get('ltc_address'):
            em.add_field(name="Payment Address", value=f"```{order['ltc_address']}```", inline=False)

        if created_at:
            em.add_field(name="Order Created", value=created_at, inline=True)
        if paid_at:
            em.add_field(name="Paid At", value=paid_at, inline=True)
        if delivered_at:
            em.add_field(name="Delivered At", value=delivered_at, inline=True)

        em.add_field(
            name="Delivery Details",
            value="Your delivered items are attached in the order file below. If you need support, please reply here.",
            inline=False,
        )
        em.set_footer(text="Thank you for your purchase! 🎉")

        # Use plain view (no buttons) since order is complete
        await msg.edit(embed=em, view=None)
        logging.info(f"Updated user DM message for order {order['id']}")
    except Exception as e:
        logging.debug(f"Could not update user DM message for order {order['id']}: {e}")


async def refresh_order_message(order: dict, get_channel_callback, get_product_callback, bot_instance, build_order_embed_callback):
    """Refresh the user's order message"""
    channel_id = order.get('channel_id')
    message_id = order.get('message_id')
    if not channel_id or not message_id:
        return

    try:
        channel = await get_channel_callback(channel_id)
        if channel is None and order.get('user_id'):
            try:
                user_id_int = int(order['user_id'])
                user = bot_instance.get_user(user_id_int) or await bot_instance.fetch_user(user_id_int)
                if user:
                    channel = user.dm_channel or await user.create_dm()
            except Exception as e:
                logging.debug(f"Could not resolve DM channel for order {order['id']} user {order.get('user_id')}: {e}")

        if channel is None:
            return

        msg = await channel.fetch_message(int(message_id))
        product = get_product_callback(order['product_id'])
        if not product:
            return

        disabled = order['status'] in {'delivered', 'expired', 'failed', 'sweep_failed', 'canceled', 'refunded'}
        view = OrderCancelView(order['id'], disabled=disabled)
        await msg.edit(embed=build_order_embed_callback(order, product), view=view)
    except Exception as e:
        logging.debug(f"Could not refresh order message for order {order['id']}: {e}")


async def refresh_pending_invoice_messages(all_orders_callback, update_invoice_callback):
    """Refresh all pending invoice messages"""
    pending_orders = [order for order in all_orders_callback() if order['status'] == 'pending']
    if not pending_orders:
        return 0

    refreshed = 0
    for order in pending_orders:
        try:
            await update_invoice_callback(order, None)
            refreshed += 1
        except Exception as e:
            logging.warning(f"Could not refresh invoice for order {order['id']}: {e}")

    logging.info(f"🔁 Refreshed {refreshed}/{len(pending_orders)} pending invoice messages")
    return refreshed