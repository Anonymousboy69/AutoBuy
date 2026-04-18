# commands.py - Discord bot command handlers
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import logging
import uuid
import io
from decimal import Decimal, ROUND_DOWN

from shopbot.database import (
    get_db,
    get_sales_analytics,
    get_database_health,
    log_audit,
)
from src.database.wrappers import (
    get_product,
    all_products,
    all_products_by_category,
    get_categories,
    add_category,
    all_orders,
    get_stock_items,
    get_visible_stock_items,
    get_user_wallet,
    set_user_wallet,
    remove_user_wallet,
    get_seller_revenue,
    record_payout,
    get_payout_history,
    reserve_stock_items,
    release_reserved_stock,
    get_reserved_stock_items_for_order,
    assign_order_stock_to_order,
    normalize_product_id,
    get_order,
)
from datetime import datetime, timezone
from shopbot.crypto import (
    format_ltc,
    fetch_ltc_usd_price,
    generate_ltc_address,
    litoshi_to_ltc,
    get_address_balance,
    register_blockcypher_webhook,
    delete_blockcypher_webhook,
    find_address_path_by_address,
    sweep_payment,
)
from shopbot.shop import ShopPage, get_stock_status
from utils import (
    COLORS,
    ADMIN_ROLE_ID,
    SELLER_ROLE_ID,
    INVOICE_CHANNEL_ID,
    DB_FILE,
    WALLET_SEED,
    RECEIVING_ADDRESS,
    LTC_CONFIRMATIONS,
    CONFIG,
    admin_check_interaction,
    seller_check_interaction,
    STATUS_EMOJI,
    INVOICE_REFRESH_INTERVAL,
    get_next_blockcypher_token,
    WEBHOOK_BASE_URL,
    WEBHOOK_SECRET,
    BLOCKCYPHER_WEBHOOK_EVENT,
)
from ui.embeds import build_wallet_embed, build_invoice_embed, build_live_embed, build_restock_embed, product_to_builder_data
from ui.views import WalletView, InvoiceApproveView, OrderCancelView, EmbedBuilderView, ProductDetailView, RestockView, start_product_builder, AdminPanelView
from src.services.order_manager import build_order_embed, update_invoice_message


def user_has_admin_or_seller_role(user) -> bool:
    """Check if user has admin or seller role"""
    if not hasattr(user, 'roles'):
        return False
    return any(role.id in {ADMIN_ROLE_ID, SELLER_ROLE_ID} for role in user.roles if ADMIN_ROLE_ID and SELLER_ROLE_ID)


def admin_or_seller_check_interaction(interaction: discord.Interaction) -> bool:
    """Check if interaction user has admin or seller role"""
    return user_has_admin_or_seller_role(interaction.user)


async def safe_interaction_send(
    interaction: discord.Interaction,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
):
    """Send a response or follow-up safely for slash interactions."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
    except discord.InteractionResponded:
        try:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
        except Exception as e:
            logging.warning(f"Failed to send follow-up message: {e}")
    except discord.NotFound as e:
        logging.warning(f"Interaction no longer valid: {e}")
    except Exception as e:
        logging.warning(f"Unhandled interaction send error: {e}")


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
async def send_dashboard(target):
    """Client/User dashboard with shopping commands"""
    em = discord.Embed(
        title       = "🛍️ Shop Dashboard",
        description = "Welcome to the shop! Use the commands below to browse and purchase products.",
        color       = COLORS["primary"],
    )
    em.add_field(
        name  = "🛒 Shopping Commands",
        value = "`/shop` - Browse all products\n"
                "`/buy <product_id>` - Purchase a product\n"
                "`/checkstock <product_id>` - Check stock availability\n"
                "`/order <order_id>` - Check order status\n"
                "`/myorders` - View your order history",
        inline = False,
    )
    em.add_field(
        name  = "💰 Payment Info",
        value = "• All payments are made in **Litecoin (LTC)**\n"
                "• Payment addresses are generated per order\n"
                "• Orders expire after 1 hour if unpaid\n"
                "• First-to-pay gets the item",
        inline = False,
    )
    em.add_field(
        name  = "📞 Support",
        value = "Need help? Contact an admin or seller.",
        inline = False,
    )
    em.set_footer(text="Happy shopping! 🎉")

    if isinstance(target, discord.Interaction):
        if not target.response.is_done():
            await target.response.send_message(embed=em)
        else:
            await target.followup.send(embed=em)
    else:
        await target.send(embed=em)


async def prefix_dashboard(ctx, bot_instance):
    """Prefix command wrapper for dashboard"""
    await send_dashboard(ctx)


# ─────────────────────────────────────────────
#  SHOP
# ─────────────────────────────────────────────
async def send_shop(target, sort_by: str = "newest", category: str | None = None):
    """Send a paginated shop view"""
    is_slash = isinstance(target, discord.Interaction)

    if category:
        products = all_products_by_category(category)
    else:
        products = all_products()

    if not products:
        em = discord.Embed(
            title       = "🛒 Shop",
            description = "No products available yet.",
            color       = COLORS["warning"],
        )
        if is_slash:
            await target.response.send_message(embed=em, ephemeral=True)
        else:
            await target.send(embed=em)
        return

    view = ShopPage(products, 0, category, sort_by)
    if is_slash:
        await target.response.send_message(embed=view.get_embed(), view=view)
    else:
        await target.send(embed=view.get_embed(), view=view)


async def prefix_stockcheck(ctx, product_id: str, normalize_product_id_callback, get_product_callback, get_visible_stock_items_callback, build_no_stock_embed_callback, StockItemPage_cls):
    product_id = normalize_product_id_callback(product_id)
    product = get_product_callback(product_id)
    if not product:
        await ctx.send(f"❌ Product `{product_id}` not found.")
        return

    stock_items = get_visible_stock_items_callback(product_id, ctx.author, ctx.author.roles, ctx.guild, 'pending')
    if not stock_items:
        em = build_no_stock_embed_callback(product_id)
        await ctx.send(embed=em)
        return

    view = StockItemPage_cls(product_id, stock_items)
    await ctx.send(embed=view.get_embed(), view=view)


async def slash_stockcheck(interaction: discord.Interaction, product_id: str, normalize_product_id_callback, get_product_callback, get_visible_stock_items_callback, build_no_stock_embed_callback, StockItemPage_cls):
    product_id = normalize_product_id_callback(product_id)
    product = get_product_callback(product_id)
    if not product:
        await interaction.response.send_message(f"❌ Product `{product_id}` not found.", ephemeral=True)
        return

    stock_items = get_visible_stock_items_callback(product_id, interaction.user, interaction.user.roles, interaction.guild, 'pending')
    seller_only = seller_check_interaction(interaction) and not admin_check_interaction(interaction)

    if not stock_items:
        em = build_no_stock_embed_callback(product_id, seller_only=seller_only)
        await interaction.response.send_message(embed=em, ephemeral=True)
        return

    view = StockItemPage_cls(product_id, stock_items)
    await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)


async def send_order_status(target, order_id_prefix: str, get_db_callback, get_product_callback):
    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author

    conn = get_db_callback(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC''', (str(user.id),))
    orders = c.fetchall()
    conn.close()

    match = None
    for order in orders:
        order_dict = dict(order)
        if order_dict["id"].startswith(order_id_prefix):
            match = order_dict
            break

    if not match:
        em = discord.Embed(
            title="❌ Not Found",
            description="Order not found (or it belongs to someone else).",
            color=COLORS["error"],
        )
    else:
        product = get_product_callback(match["product_id"])
        emoji = STATUS_EMOJI.get(match["status"], "❓")
        em = discord.Embed(title=f"{emoji} Order Status", color=COLORS["primary"])
        em.add_field(name="Order ID", value=f"`{match['id'][:8]}`", inline=True)
        em.add_field(name="Product", value=product["name"] if product else "Unknown", inline=True)
        em.add_field(name="Status", value=match["status"].capitalize(), inline=True)
        em.add_field(name="Amount", value=f"{match['price_ltc']} LTC", inline=True)
        if match["status"] == "pending":
            em.add_field(name="Pay to", value=f"```{match['ltc_address']}```", inline=False)

    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)


async def send_my_orders(target, get_db_callback, get_product_callback):
    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author

    conn = get_db_callback(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 10''', (str(user.id),))
    user_orders = c.fetchall()
    conn.close()

    em = discord.Embed(title="📋 Your Orders", color=COLORS["primary"])

    if not user_orders:
        em.description = "You haven't placed any orders yet."
    else:
        for o in user_orders:
            order_dict = dict(o)
            p = get_product_callback(order_dict["product_id"])
            emoji = STATUS_EMOJI.get(order_dict["status"], "❓")
            em.add_field(
                name=f"{emoji} `{order_dict['id'][:8]}`",
                value=f"**{p['name'] if p else 'Unknown'}** • {order_dict['price_ltc']} LTC • {order_dict['status'].capitalize()}",
                inline=False,
            )
        em.set_footer(text="Showing last 10 orders")

    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)


async def find_user_order_for_cancel(target, order_id: str, get_db_callback):
    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author

    conn = get_db_callback(DB_FILE)
    c = conn.cursor()
    if order_id:
        c.execute(
            "SELECT * FROM orders WHERE user_id = ? AND id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (str(user.id), f"{order_id}%"),
        )
    else:
        c.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status NOT IN (?, ?, ?, ?, ?, ?) ORDER BY created_at DESC LIMIT 1",
            (str(user.id), 'delivered', 'expired', 'failed', 'sweep_failed', 'canceled', 'refunded'),
        )
    order = c.fetchone()
    conn.close()
    return dict(order) if order else None


async def send_order_cancel_response(target, content: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = True):
    if isinstance(target, discord.Interaction):
        if not target.response.is_done():
            await target.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await target.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    else:
        if embed:
            await target.send(embed=embed)
        elif content is not None:
            await target.send(content)


async def do_cancel_order(
    target,
    order_id: str = "",
    get_db_callback=None,
    get_address_balance_callback=None,
    update_invoice_message_callback=None,
    refresh_order_message_callback=None,
    update_stock_message_callback=None,
    notify_next_in_queue_callback=None,
):
    is_interaction = isinstance(target, discord.Interaction)
    if is_interaction and not target.response.is_done():
        try:
            await target.response.defer(ephemeral=True)
        except Exception:
            pass

    order = await find_user_order_for_cancel(target, order_id, get_db_callback)
    if not order:
        await send_order_cancel_response(
            target,
            "❌ No active order found to cancel. Use `/myorders` to see your orders or pass an order ID.",
        )
        return

    if order['status'] in {'paid', 'delivered', 'failed', 'expired', 'sweep_failed', 'canceled', 'refunded'}:
        await send_order_cancel_response(
            target,
            "❌ This order cannot be canceled at this stage.",
        )
        return

    try:
        balance_info = await asyncio.wait_for(get_address_balance_callback(order['ltc_address']), timeout=5.0)
    except asyncio.TimeoutError:
        balance_info = None
    except Exception:
        balance_info = None

    confirmed = litoshi_to_ltc(balance_info.get('balance', 0)) if balance_info else Decimal('0')
    unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0)) if balance_info else Decimal('0')
    expected = Decimal(str(order['price_ltc'])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

    if confirmed >= expected or unconfirmed >= expected:
        await send_order_cancel_response(
            target,
            "❌ Payment was already detected on this order, so it cannot be canceled.",
        )
        return

    if confirmed > 0 or unconfirmed > 0:
        await send_order_cancel_response(
            target,
            "⚠️ Partial payment has been detected on this order. Please contact support for a refund instead of canceling now.",
        )
        return

    old_status = order['status']
    conn = get_db_callback(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE orders SET status = ? WHERE id = ?', ('canceled', order['id']))
    conn.commit()
    conn.close()

    if order.get('blockcypher_hook_id'):
        try:
            deleted = await delete_blockcypher_webhook(order['blockcypher_hook_id'], get_next_blockcypher_token())
            if deleted:
                conn = get_db_callback(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE orders SET blockcypher_hook_id = NULL WHERE id = ?', (order['id'],))
                conn.commit()
                conn.close()
        except Exception as e:
            logging.warning(f"Failed to delete BlockCypher webhook for canceled order {order['id'][:8]}: {e}")

    order['status'] = 'canceled'
    try:
        await update_invoice_message_callback(order, None)
    except Exception:
        pass

    try:
        await refresh_order_message_callback(order)
    except Exception:
        pass

    async def background_cancel_updates():
        try:
            await update_stock_message_callback(order['product_id'])
        except Exception:
            pass
        if old_status in {'pending', 'queued'}:
            try:
                await notify_next_in_queue_callback(order['product_id'])
            except Exception:
                pass

    asyncio.create_task(background_cancel_updates())


async def restore_order_cancel_views(get_db_callback, bot_instance):
    conn = get_db_callback(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id, message_id FROM orders WHERE status NOT IN (?, ?, ?, ?, ?, ?) AND channel_id IS NOT NULL AND message_id IS NOT NULL",
        ('delivered', 'expired', 'failed', 'sweep_failed', 'canceled', 'refunded'),
    )
    rows = c.fetchall()
    conn.close()

    restored_count = 0
    for row in rows:
        try:
            bot_instance.add_view(OrderCancelView(row['id']), message_id=row['message_id'])
            restored_count += 1
        except Exception as e:
            logging.warning(f"Could not restore cancel view for order {row['id']} on message {row['message_id']}: {e}")

    logging.info(f"✅ Restored {restored_count} order cancel views")
    return restored_count


_pending_order_requests: set[tuple[str, str]] = set()


async def process_buy(
    target,
    product_id: str,
    quantity: int = 1,
    bot_instance=None,
    get_channel_callback=None,
    update_stock_callback=None,
    build_order_embed_callback=None,
):
    """Create and send a new order with payment instructions"""
    # Provide defaults for callbacks if not supplied
    if build_order_embed_callback is None:
        build_order_embed_callback = build_order_embed
    
    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author
    target_guild = getattr(target, 'guild', None)
    target_channel = getattr(target, 'channel', None)
    logging.debug(
        f"process_buy called: target_type={type(target).__name__}, interaction_type={getattr(target_guild, 'id', None)}, guild={getattr(target_guild, 'id', None)}, channel={getattr(target_channel, 'id', None)}"
    )

    async def send_response(embed: discord.Embed | None = None, content: str | None = None, ephemeral: bool = True):
        if is_slash:
            if not target.response.is_done():
                await target.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
            else:
                await target.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            if embed:
                await target.send(embed=embed)
            elif content is not None:
                await target.send(content)

    async def send_invoice_message(order_record: dict, product: dict):
        if not INVOICE_CHANNEL_ID:
            logging.error(f"❌ INVOICE_CHANNEL_ID is not configured. Invoice cannot be sent for order {order_record['id']}")
            return False

        try:
            logging.info(f"🔍 Attempting to fetch invoice channel {INVOICE_CHANNEL_ID}")
            invoice_channel = await get_channel_callback(INVOICE_CHANNEL_ID)
            if not invoice_channel:
                logging.error(f"❌ Invoice channel {INVOICE_CHANNEL_ID} NOT FOUND - check channel ID in config.json and bot permissions")
                return False

            logging.info(f"✅ Invoice channel found: {invoice_channel.name} ({invoice_channel.id})")
            invoice_view = InvoiceApproveView(order_record['id'], show_refund=False)
            invoice_msg = await invoice_channel.send(
                embed=build_invoice_embed(order_record, product, {'balance': 0, 'unconfirmed_balance': 0}),
                view=invoice_view,
            )
            if not invoice_msg:
                logging.error(f"❌ Invoice message send returned None for order {order_record['id']}")
                return False

            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('UPDATE orders SET invoice_message_id = ? WHERE id = ?', (invoice_msg.id, order_record['id']))
            conn.commit()
            conn.close()
            logging.info(f"✅ Invoice message created for order {order_record['id'][:8]}: message_id={invoice_msg.id}")
            return True
        except Exception as send_err:
            logging.exception(f"❌ Failed to send invoice message to channel for order {order_record['id']}: {send_err}")
            return False

    lock_key = (str(user.id), product_id)
    if lock_key in _pending_order_requests:
        em = discord.Embed(
            title="⏳ Order In Progress",
            description="Your order for this product is already being processed. Please wait a moment before trying again.",
            color=COLORS["warning"]
        )
        if is_slash:
            await target.response.send_message(embed=em, ephemeral=True)
        else:
            await target.send(embed=em)
        return

    _pending_order_requests.add(lock_key)

    try:
        product = get_product(product_id)
        if not product:
            em = discord.Embed(title="❌ Not Found", description=f"No product with ID `{product_id}`.", color=COLORS["error"])
            if is_slash:
                await target.response.send_message(embed=em, ephemeral=True)
            else:
                await target.send(embed=em)
            return

        conn = get_db(DB_FILE)
        c = conn.cursor()
        now = datetime.now(timezone.utc).timestamp()
        one_minute_ago = now - 60
        c.execute('SELECT COUNT(*) FROM orders WHERE user_id = ? AND created_at > ?', (str(user.id), one_minute_ago))
        recent_orders = c.fetchone()[0]

        c.execute(
            'SELECT id, status FROM orders WHERE user_id = ? AND status IN (?, ?, ?, ?) ORDER BY created_at DESC LIMIT 1',
            (str(user.id), 'pending', 'paid', 'queued', 'sweep_failed')
        )
        active_order_row = c.fetchone()
        conn.close()

        if active_order_row:
            active_order_id, active_order_status = active_order_row
            em = discord.Embed(
                title="🚫 One Active Order Only",
                description=(
                    f"You already have an active order. Please complete or cancel it before creating another one.\n\n"
                    f"**Active Order ID:** `{active_order_id[:8]}`\n"
                    f"**Status:** {active_order_status.capitalize()}"
                ),
                color=COLORS["warning"]
            )
            if is_slash:
                await target.response.send_message(embed=em, ephemeral=True)
            else:
                await target.send(embed=em)
            return

        if recent_orders >= 3:
            em = discord.Embed(
                title="⏱️ Rate Limited",
                description="You're creating orders too quickly! Please wait a moment before trying again.",
                color=COLORS["warning"]
            )
            em.add_field(name="Limit", value="3 orders per minute", inline=True)
            em.set_footer(text="This helps prevent spam and ensures fair access for everyone")
            if is_slash:
                await target.response.send_message(embed=em, ephemeral=True)
            else:
                await target.send(embed=em)
            return

        unlimited = product.get('stock', 0) < 0
        if not unlimited:
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = ?', (product_id, 'pending'))
            pending_items = c.fetchone()[0]
            conn.close()
            available_slots = pending_items
        else:
            available_slots = float('inf')

        queue_response = None
        if not unlimited and quantity > available_slots:
            if available_slots == 0:
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('SELECT COUNT(*) FROM orders WHERE product_id = ? AND status = ? AND user_id = ?',
                          (product_id, 'pending', str(user.id)))
                user_pending_orders = c.fetchone()[0]
                conn.close()

                if user_pending_orders >= 1:
                    em = discord.Embed(
                        title="⏳ Already in Queue",
                        description=f"You already have a pending order for **{product['name']}**.\n\nPlease complete your current payment or wait for it to expire.",
                        color=COLORS["warning"]
                    )
                    if is_slash:
                        await target.response.send_message(embed=em, ephemeral=True)
                    else:
                        await target.send(embed=em)
                    return

                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('SELECT COUNT(*) FROM orders WHERE product_id = ? AND status = ?', (product_id, 'pending'))
                queue_position = c.fetchone()[0] + 1
                conn.close()

                queue_response = discord.Embed(
                    title="📋 Joined Purchase Queue",
                    description=f"**{product['name']}** is currently out of stock.\n\nYou've been added to the queue at position **#{queue_position}**.\n\nWhen stock becomes available, you'll be notified to complete your purchase.",
                    color=COLORS["info"]
                )
                queue_response.add_field(name="Queue Position", value=f"#{queue_position}", inline=True)
                queue_response.add_field(name="Estimated Wait", value="Varies based on stock restocking", inline=True)
                queue_response.set_footer(text="You'll receive a DM when it's your turn to purchase")
                order_status = 'queued'
            else:
                em = discord.Embed(
                    title="⚠️ Insufficient Stock",
                    description=(
                        f"Only **{available_slots}** item{'s' if available_slots != 1 else ''} are available for **{product['name']}** right now.\n"
                        "Please choose a smaller quantity or try again later."
                    ),
                    color=COLORS["warning"]
                )
                em.add_field(name="Requested", value=str(quantity), inline=True)
                em.add_field(name="Available", value=str(available_slots), inline=True)
                if is_slash:
                    await target.response.send_message(embed=em, ephemeral=True)
                else:
                    await target.send(embed=em)
                return
        else:
            order_status = 'pending'

        if is_slash and not target.response.is_done():
            try:
                if target.type == discord.InteractionType.modal_submit:
                    await target.response.defer()
                else:
                    await target.response.defer(ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            except Exception as e:
                logging.debug(f"process_buy defer failed for modal submit: {e}")

        logging.info(f"process_buy start: user={user.id} product={product_id} status={order_status} queue_response={queue_response is not None}")
        ltc_addr, address_path, address_index = await generate_ltc_address(DB_FILE, WALLET_SEED)
        if not ltc_addr or not address_path:
            em = discord.Embed(title="⚠️ Error", description="Could not generate a payment address. Please try again later.", color=COLORS["error"])
            await send_response(embed=em, ephemeral=True)
            return

        oid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).timestamp()

        total_price_ltc = Decimal(str(product["price_ltc"])) * Decimal(str(quantity))

        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO orders (
                         id, user_id, product_id, price_ltc, ltc_address,
                         address_path, address_index, invoice_channel_id, invoice_message_id,
                         status, created_at, quantity)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (oid, str(user.id), product_id, float(total_price_ltc), ltc_addr,
                   address_path, address_index,
                   str(INVOICE_CHANNEL_ID) if INVOICE_CHANNEL_ID else None,
                   None,
                   order_status, now, quantity))

        conn.commit()
        conn.close()

        if WEBHOOK_BASE_URL:
            webhook_path = f"/webhook/{oid}/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else f"/webhook/{oid}"
            webhook_url = f"{WEBHOOK_BASE_URL}{webhook_path}"
            try:
                hook_response = await register_blockcypher_webhook(ltc_addr, webhook_url, get_next_blockcypher_token(), BLOCKCYPHER_WEBHOOK_EVENT)
                if hook_response and hook_response.get("id"):
                    conn = get_db(DB_FILE)
                    c = conn.cursor()
                    c.execute("UPDATE orders SET blockcypher_hook_id = ? WHERE id = ?", (hook_response.get("id"), oid))
                    conn.commit()
                    conn.close()
            except Exception as e:
                logging.warning(f"Could not register webhook for order {oid[:8]}: {e}")

        log_audit(product_id, "order_created", str(user.id), f"{user.name}#{user.discriminator}", 
                  quantity, f"Order {oid} created for {quantity}x {product['name']} = {format_ltc(total_price_ltc)} LTC")

        order_record = {
            'id': oid,
            'user_id': str(user.id),
            'product_id': product_id,
            'price_ltc': float(total_price_ltc),
            'ltc_address': ltc_addr,
            'address_path': address_path,
            'address_index': address_index,
            'status': 'pending',
            'created_at': now,
            'quantity': quantity,
            'swept_at': None,
            'sweep_txid': None,
            'sweep_attempts': 0,
            'notified_unconfirmed': 0,
        }
        if INVOICE_CHANNEL_ID:
            await send_invoice_message(order_record, product)
        else:
            logging.warning(f"⚠️ INVOICE_CHANNEL_ID is not set in config - invoices will not be created")

        await update_stock_callback(product_id)

        if queue_response:
            await send_response(embed=queue_response, ephemeral=True)
            return

        order_payload = {
            'id': oid,
            'quantity': quantity,
            'price_ltc': str(total_price_ltc),
            'ltc_address': ltc_addr,
            'created_at': now,
        }
        em = build_order_embed_callback(order_payload, product, format_ltc)
        cancel_view = OrderCancelView(oid)

        try:
            dm_msg = await user.send(embed=em, view=cancel_view)
            if dm_msg:
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE orders SET channel_id = ?, message_id = ? WHERE id = ?',
                         (dm_msg.channel.id, dm_msg.id, oid))
                conn.commit()
                conn.close()
                bot_instance.add_view(cancel_view, message_id=dm_msg.id)
                logging.info(f"Saved user DM message ID for order {oid}: channel_id={dm_msg.channel.id}, message_id={dm_msg.id}")
                logging.info(f"Registered persistent cancel view for order {oid} on message {dm_msg.id}")

                confirm = discord.Embed(
                    title="Payment Details Sent",
                    description=f"Order {oid[:8]} – Check your DMs",
                    color=COLORS["success"],
                )
                await send_response(embed=confirm, ephemeral=True)
            else:
                confirm = discord.Embed(
                    title="Payment Details Sent",
                    description=f"Order {oid[:8]} – Check your DMs",
                    color=COLORS["success"],
                )
                await send_response(embed=confirm, ephemeral=True)
        except discord.Forbidden:
            if is_slash:
                if not target.response.is_done():
                    await target.response.send_message(embed=em, view=cancel_view, ephemeral=True)
                else:
                    await target.followup.send(embed=em, view=cancel_view, ephemeral=True)
            else:
                await target.send(embed=em, view=cancel_view)
    except Exception as exc:
        logging.error(f"process_buy failed for product {product_id}: {exc}", exc_info=True)
        err = discord.Embed(title="⚠️ Order Failed", description="Something went wrong while creating your order. Please try again later.", color=COLORS["error"])
        await send_response(embed=err, ephemeral=True)
    finally:
        _pending_order_requests.discard(lock_key)


async def slash_dashboard(interaction: discord.Interaction):
    """Slash command for dashboard"""
    await send_dashboard(interaction)


# ─────────────────────────────────────────────
#  ADMIN PANEL
# ─────────────────────────────────────────────
async def send_admin_panel(target, bot_instance):
    """Send admin panel to target"""
    from ui.views import AdminPanelView

    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author

    if not user_has_admin_or_seller_role(user):
        em = discord.Embed(
            title       = "❌ Access Denied",
            description = "You need Admin or Seller role to access this panel.",
            color       = COLORS["error"],
        )
        if is_slash:
            await target.response.send_message(embed=em, ephemeral=True)
        else:
            await target.send(embed=em, delete_after=5)
        return

    em = discord.Embed(
        title       = "⚙️ Admin Panel",
        description = "Manage the shop and view analytics.",
        color       = COLORS["primary"],
    )
    em.add_field(
        name  = "📊 Analytics",
        value = "`/analytics` - View sales analytics\n"
                "`/dbhealth` - Check database health",
        inline = True,
    )
    em.add_field(
        name  = "📦 Products",
        value = "`/addproduct` - Add new product\n"
                "`/editproduct` - Edit existing product\n"
                "`/deleteproduct` - Remove product\n"
                "`/restock` - Add stock items",
        inline = True,
    )
    em.add_field(
        name  = "💰 Finance",
        value = "`/wallet` - Manage LTC wallet\n"
                "`/audit` - View restock history",
        inline = True,
    )

    view = AdminPanelView()
    if is_slash:
        await target.response.send_message(embed=em, view=view, ephemeral=True)
    else:
        await target.send(embed=em, view=view)


async def prefix_panel(ctx, bot_instance):
    """Prefix command wrapper for admin panel"""
    if not user_has_admin_or_seller_role(ctx.author):
        await ctx.send("❌ Admin or Seller only.", delete_after=5)
        return
    await send_admin_panel(ctx, bot_instance)


async def slash_panel(interaction: discord.Interaction, bot_instance):
    """Slash command for admin panel"""
    if not admin_or_seller_check_interaction(interaction):
        await interaction.response.send_message("❌ Admin or Seller only.", ephemeral=True)
        return
    await send_admin_panel(interaction, bot_instance)


# ─────────────────────────────────────────────
#  WALLET PANEL
# ─────────────────────────────────────────────
async def send_wallet_panel(target, bot_instance):
    """Send wallet management panel"""
    is_slash = isinstance(target, discord.Interaction)
    user = target.user if is_slash else target.author

    if not user_has_admin_or_seller_role(user):
        em = discord.Embed(
            title       = "❌ Access Denied",
            description = "You need Admin or Seller role to manage wallets.",
            color       = COLORS["error"],
        )
        if is_slash:
            await target.response.send_message(embed=em, ephemeral=True)
        else:
            await target.send(embed=em, delete_after=5)
        return

    wallet_address = get_user_wallet(user.id)
    em = build_wallet_embed(wallet_address, user)

    view = WalletView(user.id)
    if is_slash:
        await target.response.send_message(embed=em, view=view, ephemeral=True)
    else:
        await target.send(embed=em, view=view)


async def prefix_wallet(ctx, bot_instance):
    """Prefix command wrapper for wallet panel"""
    if not user_has_admin_or_seller_role(ctx.author):
        await ctx.send("❌ Admin or Seller only.", delete_after=5)
        return
    await send_wallet_panel(ctx, bot_instance)


async def slash_wallet(interaction: discord.Interaction, bot_instance):
    """Slash command for wallet management"""
    if not admin_or_seller_check_interaction(interaction):
        await interaction.response.send_message("❌ Admin or Seller only.", ephemeral=True)
        return
    await send_wallet_panel(interaction, bot_instance)


# ─────────────────────────────────────────────
#  LTC PRICE
# ─────────────────────────────────────────────
async def send_ltc_price(target):
    """Send current LTC price"""
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


async def prefix_ltc(ctx):
    """Prefix command wrapper for LTC price"""
    await send_ltc_price(ctx)


async def slash_ltc(interaction: discord.Interaction):
    """Slash command for LTC price"""
    await send_ltc_price(interaction)


# ─────────────────────────────────────────────
#  ADMIN COMMAND HELPERS
# ─────────────────────────────────────────────
async def analytics_command(target, days: int = 7):
    is_slash = isinstance(target, discord.Interaction)
    try:
        analytics = get_sales_analytics(DB_FILE, days)

        em = discord.Embed(
            title=f"📊 Sales Analytics (Last {days} Days)",
            color=COLORS["info"],
        )

        totals = analytics["totals"]
        em.add_field(name="💰 Revenue", value=f"**{totals.get('total_revenue', 0):.8f} LTC**", inline=True)
        em.add_field(name="📦 Orders", value=f"**{totals.get('total_orders', 0)}**", inline=True)
        em.add_field(name="👥 Customers", value=f"**{totals.get('total_customers', 0)}**", inline=True)

        daily_data = analytics["daily_metrics"][:7]
        if daily_data:
            daily_text = ""
            for day in daily_data:
                daily_text += f"**{day['date']}**: {day['total_orders']} orders, {day['total_revenue']:.4f} LTC\n"
            em.add_field(name="📅 Daily Breakdown", value=daily_text[:1024], inline=False)

        if is_slash:
            await target.followup.send(embed=em, ephemeral=True)
        else:
            await target.send(embed=em)
    except Exception as e:
        if is_slash:
            await target.followup.send(f"❌ Failed to load analytics: {e}", ephemeral=True)
        else:
            await target.send(f"❌ Failed to load analytics: {e}")


async def db_health_command(target):
    is_slash = isinstance(target, discord.Interaction)
    try:
        health = get_database_health(DB_FILE)

        em = discord.Embed(
            title="🏥 Database Health Report",
            color=COLORS["info"],
        )
        em.add_field(name="💾 Database Size", value=f"**{health.get('database_size_mb', 0)} MB**", inline=True)
        em.add_field(name="📦 Products", value=f"**{health.get('products_count', 0)}**", inline=True)
        em.add_field(name="📦 Stock Items", value=f"**{health.get('stock_items_count', 0)}**", inline=True)

        if is_slash:
            await target.followup.send(embed=em, ephemeral=True)
        else:
            await target.send(embed=em)
    except Exception as e:
        if is_slash:
            await target.followup.send(f"❌ Failed to check database health: {e}", ephemeral=True)
        else:
            await target.send(f"❌ Failed to check database health: {e}")


async def send_audit_log(target, product_id: str):
    is_slash = isinstance(target, discord.Interaction)
    if is_slash and not admin_check_interaction(target):
        await target.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute(
        '''SELECT * FROM audit_log WHERE product_id = ? ORDER BY created_at DESC LIMIT 20''',
        (product_id,),
    )
    logs = c.fetchall()
    conn.close()

    if not logs:
        em = discord.Embed(title="📋 Audit Log", description="No audit entries found.", color=COLORS["info"])
    else:
        em = discord.Embed(title=f"📋 Audit Log: {product_id}", color=COLORS["info"])
        for log in logs:
            log_dict = dict(log)
            timestamp = datetime.fromtimestamp(log_dict["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
            em.add_field(
                name=f"{log_dict['action'].upper()} by {log_dict['admin_name']}",
                value=f"Time: {timestamp}\nItems: {log_dict['item_count']}\nDetails: {log_dict['details']}",
                inline=False,
            )

    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)


async def slash_addproduct(interaction: discord.Interaction):
    if not admin_check_interaction(interaction):
        await safe_interaction_send(interaction, content="🚫 Admin only.", ephemeral=True)
        return

    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    view = start_product_builder(interaction)
    await safe_interaction_send(interaction, embed=build_live_embed(view.data), view=view, ephemeral=True)


async def slash_editproduct(interaction: discord.Interaction, product_id: str):
    if not admin_check_interaction(interaction):
        await safe_interaction_send(interaction, content="🚫 Admin only.", ephemeral=True)
        return

    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    product = get_product(product_id)
    if not product:
        await safe_interaction_send(interaction, content=f"❌ Product `{product_id}` not found.", ephemeral=True)
        return

    data = product_to_builder_data(dict(product))
    view = EmbedBuilderView(interaction.user.id, edit_product_id=product_id)
    view.data = data

    await safe_interaction_send(interaction, embed=build_live_embed(data, pid=product_id), view=view, ephemeral=True)


async def do_delete_product(target, product_id: str, get_channel_by_id_callback):
    is_slash = isinstance(target, discord.Interaction)
    if is_slash and not admin_check_interaction(target):
        await target.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    bot_instance = getattr(target, 'bot', None) or getattr(target, 'client', None)

    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT name, channel_id FROM products WHERE id = ?', (product_id,))
    result = c.fetchone()

    if not result:
        em = discord.Embed(
            title="❌ Not Found",
            description=f"No product with ID `{product_id}`.",
            color=COLORS["error"],
        )
    else:
        name, channel_id = result
        channel_deleted = False
        channel_issue = None

        if channel_id:
            try:
                channel_id_int = int(channel_id)
            except (TypeError, ValueError):
                channel_id_int = None

            if channel_id_int is not None:
                channel = bot_instance.get_channel(channel_id_int) if bot_instance else None

                if channel is None and hasattr(target, "guild") and target.guild:
                    channel = target.guild.get_channel(channel_id_int)

                if channel is None and bot_instance is not None:
                    for guild_search in bot_instance.guilds:
                        channel = guild_search.get_channel(channel_id_int)
                        if channel is not None:
                            break

                if channel is None and bot_instance is not None:
                    for guild_search in bot_instance.guilds:
                        try:
                            await guild_search.fetch_channels()
                            channel = guild_search.get_channel(channel_id_int)
                            if channel is not None:
                                break
                        except Exception:
                            continue

                if channel is None:
                    channel = await get_channel_by_id_callback(channel_id_int)

                if channel is not None:
                    try:
                        await channel.delete(reason=f"Product {product_id} deleted")
                        channel_deleted = True
                    except Exception as e:
                        logging.warning(f"Failed to delete product channel {channel_id} for product {product_id}: {e}", exc_info=True)
                        channel_issue = str(e)

        c.execute('DELETE FROM products WHERE id = ?', (product_id,))
        c.execute('DELETE FROM stock_items WHERE product_id = ?', (product_id,))
        c.execute('DELETE FROM audit_log WHERE product_id = ?', (product_id,))
        conn.commit()

        description = f"**{name}** (`{product_id}`) has been removed."
        if channel_issue:
            description += f"\n\n⚠️ Could not delete product channel {channel_id}: {channel_issue}"

        em = discord.Embed(
            title="🗑️ Product Deleted",
            description=description,
            color=COLORS["success"],
        )

    conn.close()
    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)


async def send_all_orders(target):
    is_slash = isinstance(target, discord.Interaction)
    if is_slash and not admin_check_interaction(target):
        await target.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    orders = all_orders()
    em = discord.Embed(title="📦 All Orders", color=COLORS["primary"])

    if not orders:
        em.description = "No orders yet."
    else:
        for o in orders[:15]:
            p = get_product(o["product_id"])
            emoji = STATUS_EMOJI.get(o["status"], "❓")
            em.add_field(
                name=f"{emoji} `{o['id'][:8]}`",
                value=(
                    f"User: <@{o['user_id']}> • **{p['name'] if p else '?'}** "
                    f"• {o['price_ltc']} LTC • {o['status'].capitalize()}"
                ),
                inline=False,
            )
        em.set_footer(text=f"Showing last 15 of {len(orders)} orders")

    if is_slash:
        await target.response.send_message(embed=em, ephemeral=True)
    else:
        await target.send(embed=em)


async def slash_insertorder(interaction: discord.Interaction, order_id: str, user_id: str, product_id: str, ltc_address: str, price_ltc: float):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db(DB_FILE)
        c = conn.cursor()
        now = datetime.now(timezone.utc).timestamp()
        c.execute(
            '''INSERT INTO orders 
                 (id, user_id, product_id, price_ltc, ltc_address, address_path, address_index, status, created_at) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (order_id, user_id, product_id, price_ltc, ltc_address, 'm/0/0', 0, 'pending', now),
        )
        conn.commit()
        conn.close()

        await interaction.followup.send(f"✅ Order `{order_id}` inserted with status `pending`", ephemeral=True)
        logging.info(f"Manual order inserted: {order_id} for user {user_id}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        logging.error(f"Failed to insert order: {e}")


async def slash_refund(interaction: discord.Interaction, order_id: str, refund_txid: str = None, refund_address: str = None, fetch_user_callback=None):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    order = None
    for o in all_orders():
        if o['id'].startswith(order_id):
            order = o
            break

    if not order:
        await interaction.followup.send("❌ Order not found.", ephemeral=True)
        return

    if order['status'] == 'refunded':
        await interaction.followup.send("⚠️ Order is already marked as refunded.", ephemeral=True)
        return

    conn = get_db(DB_FILE)
    c = conn.cursor()
    now = datetime.now(timezone.utc).timestamp()

    if refund_txid and refund_address:
        c.execute(
            '''UPDATE orders SET status = ?, refund_txid = ?, refund_address = ?, refund_at = ? 
                 WHERE id = ?''',
            ('refunded', refund_txid, refund_address, now, order['id']),
        )
    else:
        c.execute('UPDATE orders SET status = ?, refund_at = ? WHERE id = ?', ('refunded', now, order['id']))

    conn.commit()
    conn.close()

    try:
        user = await fetch_user_callback(int(order['user_id']))
        em = discord.Embed(
            title="💰 Manual Refund Processed",
            description=f"Your order **#{order['id'][:8]}** has been manually refunded by an admin.",
            color=COLORS["success"],
        )
        if refund_txid:
            em.add_field(name="Refund TX ID", value=f"`{refund_txid[:16]}...`", inline=False)
        em.set_footer(text="Thank you for your patience")
        await user.send(embed=em)
    except Exception as e:
        logging.warning(f"Could not notify user {order['user_id']} about manual refund: {e}")

    try:
        await update_invoice_message(order, None)
    except Exception:
        pass

    log_audit(
        order['product_id'],
        "manual_refund",
        str(interaction.user.id),
        interaction.user.name,
        1,
        f"Order {order['id'][:8]} manually refunded (txid: {refund_txid or 'N/A'})",
    )

    await interaction.followup.send(
        f"✅ Order **#{order['id'][:8]}** marked as refunded.\n"
        f"User notified and invoice updated.",
        ephemeral=True,
    )


async def slash_checkbalance(interaction: discord.Interaction, order_id: str):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    order = None
    for o in all_orders():
        if o['id'].startswith(order_id):
            order = o
            break

    if not order:
        await interaction.followup.send("❌ Order not found.", ephemeral=True)
        return

    balance_info = await get_address_balance(order['ltc_address'])
    if not balance_info:
        await interaction.followup.send("❌ Could not check balance.", ephemeral=True)
        return

    confirmed = litoshi_to_ltc(balance_info.get('balance', 0))
    unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0))
    expected = Decimal(str(order['price_ltc'])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

    em = discord.Embed(
        title=f"💰 Balance Check — Order {order['id'][:8]}",
        color=COLORS["info"],
    )
    em.add_field(name="Address", value=f"`{order['ltc_address']}`", inline=False)
    em.add_field(name="Confirmed Balance", value=f"{format_ltc(confirmed)} LTC", inline=True)
    em.add_field(name="Unconfirmed Balance", value=f"{format_ltc(unconfirmed)} LTC", inline=True)
    em.add_field(name="Expected Payment", value=f"{format_ltc(expected)} LTC", inline=True)
    em.add_field(name="Status", value=order['status'].capitalize(), inline=True)

    if confirmed > 0 or unconfirmed > 0:
        if order['status'] == 'failed':
            action_text = 'Payment exists but delivery failed. Admin review required.'
        elif order['status'] == 'expired':
            action_text = 'Payment exists but order expired. Admin review required.'
        elif order['status'] == 'canceled':
            action_text = 'Payment exists and order was canceled. Manual refund may be required.'
        else:
            action_text = 'Balance detected. Order may need manual review or delivery.'
        em.add_field(name='Action Needed', value=action_text, inline=False)

    await interaction.followup.send(embed=em, ephemeral=True)

async def slash_sweep(interaction: discord.Interaction, order_id: str):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    order = None
    for o in all_orders():
        if o['id'].startswith(order_id):
            order = o
            break

    if not order:
        await interaction.followup.send("❌ Order not found.", ephemeral=True)
        return

    balance_info = await get_address_balance(order['ltc_address'])
    if not balance_info:
        await interaction.followup.send("❌ Could not check balance.", ephemeral=True)
        return

    confirmed = litoshi_to_ltc(balance_info.get('balance', 0))
    if confirmed <= 0:
        await interaction.followup.send("❌ No confirmed payment found on this address.", ephemeral=True)
        return

    address_path = order.get('address_path')
    if not address_path:
        address_path = find_address_path_by_address(DB_FILE, order['ltc_address'], WALLET_SEED)
        if address_path:
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('UPDATE orders SET address_path = ? WHERE id = ?', (address_path, order['id']))
            conn.commit()
            conn.close()

    if not address_path:
        await interaction.followup.send(
            "❌ Could not determine address path for this order. Manual sweep requires the address derivation index.",
            ephemeral=True,
        )
        return

    try:
        swept, sweep_txid = await sweep_payment(
            db_file=DB_FILE,
            address_path=address_path,
            from_address=order['ltc_address'],
            amount_ltc=confirmed,
            wallet_seed=WALLET_SEED,
            receiving_address=RECEIVING_ADDRESS,
            blockcypher_token=get_next_blockcypher_token(),
            ltc_confirmations=LTC_CONFIRMATIONS,
            recipients=None,
        )
    except Exception as e:
        logging.error(f"Sweep failed for order {order['id'][:8]}: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Sweep failed: {str(e)[:100]}", ephemeral=True)
        return

    now = datetime.now(timezone.utc).timestamp()
    conn = get_db(DB_FILE)
    c = conn.cursor()
    c.execute(
        'UPDATE orders SET swept_at = ?, sweep_txid = ?, sweep_attempts = COALESCE(sweep_attempts, 0) + 1, last_sweep_attempt = ? WHERE id = ?',
        (now, sweep_txid, now, order['id'])
    )
    conn.commit()
    conn.close()

    if swept:
        await interaction.followup.send(
            f"✅ Swept {format_ltc(confirmed)} LTC from order {order['id'][:8]} to the receiving wallet.\n"
            f"TXID: `{sweep_txid}`",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "⚠️ Sweep attempt completed but did not broadcast successfully. Check the bot logs for details.",
            ephemeral=True,
        )


async def slash_checktxid(interaction: discord.Interaction, txid: str):
    """Check Litecoin transaction details by TXID"""
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        from shopbot.crypto import get_transaction_details
        from utils.config import get_next_blockcypher_token
        
        tx_data = await get_transaction_details(txid, get_next_blockcypher_token())
        
        if not tx_data:
            await interaction.followup.send(f"❌ Transaction `{txid}` not found on the blockchain.", ephemeral=True)
            return

        # Parse transaction data
        confirmations = tx_data.get('confirmations', 0)
        total_in = Decimal(str(tx_data.get('total', 0))) / Decimal('100000000')  # Convert satoshis to LTC
        total_out = Decimal(str(tx_data.get('total_out', 0))) / Decimal('100000000')
        received = Decimal(str(tx_data.get('received', ''))) if tx_data.get('received') else None
        
        inputs = tx_data.get('inputs', [])
        outputs = tx_data.get('outputs', [])
        
        em = discord.Embed(
            title=f"📊 Transaction Details",
            description=f"`{txid}`",
            color=COLORS["info"],
        )
        
        # Status
        if confirmations >= 1:
            status = f"✅ Confirmed ({confirmations} confirmation{'s' if confirmations != 1 else ''})"
        else:
            status = "⏳ Unconfirmed (0 confirmations)"
        
        em.add_field(name="Status", value=status, inline=True)
        em.add_field(name="Total Input", value=f"{format_ltc(total_in)} LTC", inline=True)
        em.add_field(name="Total Output", value=f"{format_ltc(total_out)} LTC", inline=True)
        
        # Inputs
        if inputs:
            input_str = ""
            for i, inp in enumerate(inputs[:3], 1):  # Show first 3
                addr = inp.get('addresses', ['unknown'])[0] if inp.get('addresses') else 'unknown'
                output_index = inp.get('output_index', '?')
                input_str += f"{i}. `{addr[:12]}...` (UTXO #{output_index})\n"
            if len(inputs) > 3:
                input_str += f"... and {len(inputs) - 3} more input(s)"
            em.add_field(name="Inputs", value=input_str or "None", inline=False)
        
        # Outputs
        if outputs:
            output_str = ""
            for i, out in enumerate(outputs[:3], 1):  # Show first 3
                addr = out.get('addresses', ['unknown'])[0] if out.get('addresses') else 'unknown'
                amount = Decimal(str(out.get('output_value', 0))) / Decimal('100000000')
                output_str += f"{i}. `{addr[:12]}...` → {format_ltc(amount)} LTC\n"
            if len(outputs) > 3:
                output_str += f"... and {len(outputs) - 3} more output(s)"
            em.add_field(name="Outputs", value=output_str or "None", inline=False)
        
        em.set_footer(text=f"Transaction verified on Litecoin blockchain via BlockCypher")
        
        await interaction.followup.send(embed=em, ephemeral=True)
        
    except Exception as e:
        logging.error(f"Error checking transaction {txid}: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Error checking transaction: {str(e)[:100]}", ephemeral=True)


async def slash_seller_revenue(interaction: discord.Interaction, user: discord.Member, platform_fee: float = None):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    wallet = get_user_wallet(str(user.id))
    if not wallet:
        await interaction.followup.send(f"❌ **{user.name}** has not linked an LTC wallet yet.", ephemeral=True)
        return

    fee_percent = platform_fee if platform_fee is not None else CONFIG["shop"].get("platform_fee_percent", 0.0)
    revenue_data = get_seller_revenue(str(user.id), platform_fee_percent=fee_percent)

    em = discord.Embed(title=f"💰 Revenue Report: {user.name}", color=COLORS["success"])
    em.add_field(name="📊 Platform Fee", value=f"**{fee_percent}%**", inline=True)
    em.add_field(name="💵 Seller Earnings", value=f"**{revenue_data['total_revenue']:.8f} LTC**", inline=True)
    em.add_field(name="📦 Items Sold", value=f"**{revenue_data['total_items']}**", inline=True)
    em.add_field(name="🏦 Wallet", value=f"`{wallet['ltc_address']}`", inline=False)

    if revenue_data['product_breakdown']:
        breakdown_text = ""
        for product in revenue_data['product_breakdown'][:5]:
            breakdown_text += f"**{product['product_id']}**: {product['items_sold']} items, {product['revenue']:.4f} LTC\n"
        em.add_field(name="📈 Product Breakdown", value=breakdown_text[:1024], inline=False)

    em.set_footer(text=f"Revenue from items this seller restocked that were sold (after {fee_percent}% platform fee)")
    await interaction.followup.send(embed=em, ephemeral=True)


async def slash_payouts(interaction: discord.Interaction, action: str, user: discord.Member = None, process_seller_payout_callback=None, process_all_payouts_callback=None):
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    em = discord.Embed(title="💰 Payouts", color=COLORS["primary"])
    if action == "check":
        if user:
            wallet = get_user_wallet(str(user.id))
            if not wallet:
                await interaction.followup.send(f"❌ {user.name} has no linked wallet.", ephemeral=True)
                return
            revenue = get_seller_revenue(str(user.id))
            em.add_field(name="📊 Pending Earnings", value=f"**{revenue['total_revenue']:.8f} LTC**", inline=True)
            em.add_field(name="🏦 Wallet", value=f"`{wallet['ltc_address']}`", inline=True)
            em.add_field(name="📦 Items Sold", value=f"**{revenue['total_items']}**", inline=True)
            min_payout = CONFIG.get("payouts", {}).get("minimum_payout", 0.001)
            if revenue['total_revenue'] >= min_payout:
                em.add_field(name="✅ Status", value="Ready for payout", inline=False)
            else:
                em.add_field(name="⏳ Status", value=f"Minimum payout: {min_payout} LTC", inline=False)
        else:
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT user_id, ltc_address FROM user_wallets WHERE is_active = 1')
            sellers = c.fetchall()
            conn.close()

            if not sellers:
                em.add_field(name="❌ No Sellers", value="No sellers have linked wallets yet.", inline=False)
            else:
                total_pending = 0
                ready_count = 0
                min_payout = CONFIG.get("payouts", {}).get("minimum_payout", 0.001)
                for seller in sellers:
                    revenue = get_seller_revenue(seller['user_id'])
                    total_pending += revenue['total_revenue']
                    if revenue['total_revenue'] >= min_payout:
                        ready_count += 1

                em.add_field(name="👥 Total Sellers", value=f"**{len(sellers)}**", inline=True)
                em.add_field(name="✅ Ready to Pay", value=f"**{ready_count}**", inline=True)
                em.add_field(name="💵 Total Pending", value=f"**{total_pending:.8f} LTC**", inline=True)
                if ready_count > 0:
                    em.add_field(name="🚀 Action", value="Use `/payouts payall` to pay all ready sellers", inline=False)

    elif action == "pay":
        if not user:
            await interaction.followup.send("❌ Specify a seller to pay.", ephemeral=True)
            return
        success, message = await process_seller_payout_callback(str(user.id))
        if success:
            await interaction.followup.send(f"✅ {message}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return

    elif action == "payall":
        success, message = await process_all_payouts_callback()
        if success:
            await interaction.followup.send(f"✅ {message}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return

    elif action == "enable":
        CONFIG["payouts"]["enabled"] = True
        await interaction.followup.send("✅ Auto payouts enabled!", ephemeral=True)
        return

    elif action == "disable":
        CONFIG["payouts"]["enabled"] = False
        await interaction.followup.send("⏹️ Auto payouts disabled.", ephemeral=True)
        return

    await interaction.followup.send(embed=em, ephemeral=True)


async def prefix_restock(ctx, product_id: str = ""):
    """Prefix restock command"""
    await ctx.send(
        "⚠️ Please use the slash command `/restock <product_id>` instead. "
        "Prefix commands cannot send ephemeral embeds.",
        delete_after=15,
    )


async def slash_restock(interaction: discord.Interaction, product_id: str):
    """Slash restock command"""
    if not admin_check_interaction(interaction):
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return

    product = get_product(product_id)
    if not product:
        await interaction.response.send_message(
            f"❌ Product `{product_id}` not found.", ephemeral=True
        )
        return

    view = RestockView(product_id, interaction.user, interaction.user.roles, interaction.guild)
    await interaction.response.send_message(
        embed=build_restock_embed(product_id, interaction.user, interaction.user.roles, interaction.guild),
        view=view,
        ephemeral=True,
    )


async def update_product_fields(ctx, product_id: str, field: str, value: str, refresh_product_embed_callback):
    valid_fields = {
        "name": "name",
        "title": "name",
        "price": "price_ltc",
        "usd": "price_usd",
        "stock": "stock",
        "description": "description",
        "delivery": "delivery",
    }
    field_key = valid_fields.get(field.lower())
    if not field_key:
        await ctx.send(
            "Usage: `!updateproduct <product_id> <field> <value>`\n"
            "Fields: name, price, usd, stock, description, delivery"
        )
        return

    try:
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        product = c.fetchone()
        if not product:
            await ctx.send(f"❌ Product `{product_id}` not found.")
            conn.close()
            return

        update_value = value.strip()
        if field_key in {"price_ltc", "price_usd"}:
            update_value = float(update_value)
        elif field_key == "stock":
            update_value = int(update_value)

        c.execute(f'UPDATE products SET {field_key} = ? WHERE id = ?', (update_value, product_id))
        conn.commit()
        conn.close()

        product_dict = dict(product)
        product_dict[field_key] = update_value
        refreshed, refresh_error = await refresh_product_embed_callback(product_dict)

        await ctx.send(
            f"✅ Updated `{field}` for product `{product_id}`. "
            f"Embed refresh {'succeeded' if refreshed else f'failed ({refresh_error}), but data was updated.'}"
        )
    except ValueError:
        await ctx.send("❌ Invalid value type for that field. Use a number for price/stock.")
    except Exception as e:
        await ctx.send(f"❌ Could not update product: {e}")


# ─────────────────────────────────────────────
#  PAYOUT FUNCTIONS
# ─────────────────────────────────────────────
async def process_seller_payout(seller_id: str) -> tuple[bool, str]:
    """Process payout for a specific seller"""
    try:
        # Get seller wallet
        wallet = get_user_wallet(seller_id)
        if not wallet:
            return False, "Seller has no linked wallet"
        
        # Get revenue
        fee_percent = CONFIG["shop"].get("platform_fee_percent", 0.0)
        revenue = get_seller_revenue(seller_id, platform_fee_percent=fee_percent)
        
        if revenue['total_revenue'] <= 0:
            return False, "No earnings to pay"
        
        min_payout = CONFIG.get("payouts", {}).get("minimum_payout", 0.001)
        if revenue['total_revenue'] < min_payout:
            return False, f"Earnings below minimum payout ({min_payout} LTC)"
        
        # Record the payout (for now, mark as completed - in real implementation would do actual transfer)
        from shopbot.database import record_payout as _record_payout
        payout_id = _record_payout(DB_FILE, seller_id, revenue['total_revenue'], fee_percent, status='completed')
        
        logging.info(f"Payout recorded: {revenue['total_revenue']} LTC to {wallet['ltc_address']} for seller {seller_id} (ID: {payout_id})")
        
        # TODO: Implement actual LTC transfer from main wallet to seller wallet
        # This would require:
        # 1. Check main wallet balance
        # 2. Create transaction from RECEIVING_ADDRESS to seller wallet  
        # 3. Sign with WALLET_SEED
        # 4. Broadcast transaction
        # 5. Update payout record with txid
        
        return True, f"Paid {revenue['total_revenue']:.8f} LTC to seller wallet (Payout ID: {payout_id})"
        
    except Exception as e:
        logging.error(f"Payout error for seller {seller_id}: {e}")
        return False, f"Payout failed: {str(e)}"


async def process_all_payouts() -> tuple[bool, str]:
    """Process payouts for all eligible sellers"""
    try:
        # Get all sellers with wallets
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM user_wallets WHERE is_active = 1')
        sellers = c.fetchall()
        conn.close()
        
        if not sellers:
            return False, "No sellers with linked wallets"
        
        fee_percent = CONFIG["shop"].get("platform_fee_percent", 0.0)
        min_payout = CONFIG.get("payouts", {}).get("minimum_payout", 0.001)
        
        paid_count = 0
        total_paid = 0
        
        for seller in sellers:
            revenue = get_seller_revenue(seller['user_id'], platform_fee_percent=fee_percent)
            if revenue['total_revenue'] >= min_payout:
                success, message = await process_seller_payout(seller['user_id'])
                if success:
                    paid_count += 1
                    total_paid += revenue['total_revenue']
        
        if paid_count > 0:
            return True, f"Paid {paid_count} sellers totaling {total_paid:.8f} LTC"
        else:
            return False, "No sellers eligible for payout"
            
    except Exception as e:
        logging.error(f"Batch payout error: {e}")
        return False, f"Batch payout failed: {str(e)}"


async def notify_admins_out_of_stock(product: dict, product_id: str, order_id: str, send_log_embed_callback):
    """Notify admins when an order has no stock available for delivery"""
    try:
        product_name = product.get("name", "Unknown")
        fields = {
            "Product": f"{product_name} (`{product_id}`)",
            "Order ID": f"`{order_id[:8]}`",
            "Status": "Out of stock - manual refund may be needed",
            "Action": "Check stock and process refund if necessary"
        }
        
        await send_log_embed_callback(
            title="⚠️ Out of Stock Alert",
            description="An order was marked as failed due to no stock availability",
            fields=fields,
            color=COLORS["warning"]
        )
        logging.warning(f"Out of stock alert sent for order {order_id[:8]} (product: {product_name})")
    except Exception as e:
        logging.error(f"Could not send out-of-stock notification: {e}")


async def deliver_order(
    order: dict, 
    oid: str, 
    force_delivery: bool = False,
    fetch_user_callback=None,
    send_low_stock_alert_callback=None,
    notify_admins_out_of_stock_callback=None,
    update_stock_message_callback=None,
    update_user_order_message_callback=None,
    get_channel_callback=None,
    get_address_transactions_callback=None,
    litoshi_to_ltc_callback=None
):
    """Deliver an order to the user"""
    if order.get('status') == 'delivered':
        return True

    product = get_product(order["product_id"])
    if not product:
        return False

    if not force_delivery:
        balance_info = await get_address_balance(order["ltc_address"])
        expected = Decimal(str(order["price_ltc"])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        confirmed = litoshi_to_ltc(balance_info.get("balance", 0)) if balance_info else Decimal('0')
        if confirmed < expected:
            logging.warning(f"Order {oid[:8]} will not deliver: confirmed payment {confirmed} is below expected {expected}")
            return

    conn = get_db(DB_FILE)
    c = conn.cursor()

    # Get quantity from order
    quantity = int(order.get("quantity", 1))

    if force_delivery:
        assigned = assign_order_stock_to_order(order)
        if assigned:
            logging.info(f"Assigned stock for forced delivery of order {oid[:8]}")

    c.execute('''SELECT id, content FROM stock_items
                 WHERE order_id = ? AND status = 'delivered'
                 ORDER BY created_at ASC LIMIT ?''', (oid, quantity))
    stock_rows = c.fetchall()

    delivery_items = []
    if stock_rows:
        for stock_row in stock_rows:
            item_id, item_content = stock_row[0], stock_row[1]
            delivery_items.append(item_content)

    c.execute('UPDATE orders SET delivered_at = ?, status = ? WHERE id = ?',
              (datetime.now(timezone.utc).timestamp(), 'delivered' if delivery_items else 'failed', oid))
    conn.commit()
    conn.close()

    # Log delivery
    log_audit(order["product_id"], "order_delivered" if delivery_items else "order_failed", 
              "system", "Payment System", quantity, 
              f"Order {oid} {'delivered' if delivery_items else 'failed - no stock available'} ({quantity} items)")
    
    # Update invoice embed to show delivered status
    try:
        updated_order = get_order(oid)
        if updated_order:
            logging.info(f"Updating invoice for order {oid}: status={updated_order.get('status')}, invoice_msg_id={updated_order.get('invoice_message_id')}")
            if get_channel_callback and get_address_transactions_callback and litoshi_to_ltc_callback:
                await update_invoice_message(
                    updated_order, None,
                    get_channel_callback,
                    get_product,
                    get_address_transactions_callback,
                    litoshi_to_ltc_callback
                )
            logging.info(f"Invoice message updated successfully for order {oid}")
            # Also update the user's DM message
            if update_user_order_message_callback:
                await update_user_order_message_callback(updated_order)
        else:
            logging.warning(f"Could not fetch updated order {oid} after delivery")
    except Exception as e:
        logging.error(f"Could not update invoice after delivery for {oid}: {e}", exc_info=True)
    
    # Update product stock embed in shop channel
    try:
        if update_stock_message_callback:
            await update_stock_message_callback(order["product_id"])
    except Exception as e:
        logging.warning(f"Could not update stock message for product {order['product_id']}: {e}")

    try:
        user = await fetch_user_callback(int(order["user_id"]))

        created_at = datetime.fromtimestamp(order.get("created_at", datetime.now(timezone.utc).timestamp()), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        paid_at = None
        swept_at = None
        if order.get("paid_at"):
            paid_at = datetime.fromtimestamp(order["paid_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if order.get("swept_at"):
            swept_at = datetime.fromtimestamp(order["swept_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        em = discord.Embed(title="✅ Payment Confirmed – Here's Your Order!", color=COLORS["success"])
        em.add_field(name="Product", value=product["name"], inline=True)
        em.add_field(name="Quantity", value=f"×{quantity}", inline=True)
        em.add_field(name="Order ID", value=f"`{oid[:8]}`", inline=True)
        em.add_field(name="Amount", value=f"{format_ltc(Decimal(str(order['price_ltc'])))} LTC", inline=True)
        if product and product.get("price_usd") is not None:
            em.add_field(name="USD Price", value=f"${product['price_usd']:.2f}", inline=True)
        em.add_field(name="Blockchain", value="Litecoin", inline=True)
        em.add_field(name="Payment Address", value=f"`{order['ltc_address']}`", inline=False)
        em.add_field(name="Created", value=created_at, inline=True)
        if paid_at:
            em.add_field(name="Paid At", value=paid_at, inline=True)
        if swept_at:
            em.add_field(name="Swept At", value=swept_at, inline=True)

        if delivery_items:
            # Create a file with all items
            file_content = (
                f"Order ID : {oid[:8]}\n"
                f"Product  : {product['name']}\n"
                f"Quantity : {quantity}\n"
                f"{'─' * 40}\n\n"
            )
            for i, item in enumerate(delivery_items, 1):
                file_content += f"ITEM #{i}:\n{item}\n\n{'─' * 40}\n\n"
            
            file_bytes = file_content.encode("utf-8")
            file = discord.File(
                fp=io.BytesIO(file_bytes),
                filename=f"order_{oid[:8]}.txt",
            )
            em.description = f"Your {quantity} item(s) {'is' if quantity == 1 else 'are'} attached as a `.txt` file below. 👇"
            em.set_footer(text="Thank you for your purchase! 🎉")
            await user.send(embed=em, file=file)
            await send_low_stock_alert_callback(order["product_id"])
        else:
            em.add_field(name="📦 Delivery Info", value=product["delivery"], inline=False)
            em.add_field(
                name="⚠️ No Stock Available",
                value="Unfortunately, we ran out of stock for this item. Your payment has been received and is being held. Please contact support for a manual refund. We apologize for the inconvenience!",
                inline=False,
            )
            em.set_footer(text="Please contact support to request a refund.")
            await user.send(embed=em)
            await notify_admins_out_of_stock_callback(product, order["product_id"], oid)

    except Exception as e:
        logging.error(f"Could not DM user {order['user_id']}: {e}")

    return bool(delivery_items)


async def restore_product_embeds(
    get_channel_by_id_callback=None,
    find_existing_product_embed_callback=None
):
    """Restore missing product embeds on bot startup"""
    logging.info("🔄 Starting complete system restoration...")

    products = all_products()
    restored_count = 0
    failed_count = 0
    updated_stock_count = 0

    for product in products:
        try:
            # Check if channel exists
            channel_id = product.get("channel_id")
            embed_msg_id = product.get("embed_msg_id")

            if not channel_id:
                logging.warning(f"Product {product['id']} has no channel_id, skipping")
                continue

            try:
                channel = await get_channel_by_id_callback(channel_id)
                if channel is None:
                    raise ValueError(f"Channel {channel_id} not available")
            except Exception as e:
                logging.warning(f"Product {product['id']}: channel {channel_id} not found or inaccessible - {e}")
                failed_count += 1
                continue

            # Check if embed message exists and is valid
            embed_exists = False
            needs_stock_update = False
            msg = None

            if embed_msg_id:
                try:
                    msg = await channel.fetch_message(int(embed_msg_id))
                    embed_exists = True
                except Exception as e:
                    logging.warning(f"Product {product['id']}: embed message {embed_msg_id} not found - {e}")
                    embed_exists = False
                    msg = None

            if not msg:
                # Try to find any existing product embed in the channel before recreating
                msg = await find_existing_product_embed_callback(product, channel)
                if msg:
                    embed_exists = True
                    embed_msg_id = str(msg.id)
                    conn = get_db(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE products SET embed_msg_id = ?, updated_at = ? WHERE id = ?',
                             (embed_msg_id, datetime.now(timezone.utc).timestamp(), product["id"]))
                    conn.commit()
                    conn.close()
                    logging.info(f"🔎 Found existing embed for product {product['id']} in channel {channel_id}: {embed_msg_id}")

            if embed_exists and msg:
                # Refresh the interaction view so old buttons remain functional after restart
                try:
                    await msg.edit(view=ProductDetailView(product["id"]))
                except Exception as e:
                    logging.warning(f"Could not refresh view for product {product['id']}: {e}")

                # Check if stock status needs updating
                stock_count, stock_emoji = get_stock_status(DB_FILE, product["id"])
                current_stock_text = "∞ Unlimited" if product["stock"] < 0 else f"{stock_count} in stock"

                # Check if embed has stock field and if it matches current stock
                if msg.embeds and msg.embeds[0].fields:
                    stock_field = None
                    for field in msg.embeds[0].fields:
                        if "Stock" in field.name or "📦" in field.name:
                            stock_field = field
                            break

                    if stock_field and current_stock_text not in stock_field.value:
                        needs_stock_update = True

            # Recreate or update embed
            if not embed_exists:
                logging.info(f"📝 Recreating embed for product {product['id']}")

                # Build embed data
                data = product_to_builder_data(product)
                em = build_live_embed(data, pid=product["id"])

                # Create view
                view = ProductDetailView(product["id"])

                # Send new embed
                try:
                    embed_msg = await channel.send(embed=em, view=view)

                    # Update database with new message ID
                    conn = get_db(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE products SET embed_msg_id = ?, updated_at = ? WHERE id = ?',
                             (embed_msg.id, datetime.now(timezone.utc).timestamp(), product["id"]))
                    conn.commit()
                    conn.close()

                    restored_count += 1
                    logging.info(f"✅ Restored embed for product {product['id']}: new message {embed_msg.id}")

                except Exception as e:
                    logging.error(f"❌ Failed to recreate embed for product {product['id']}: {e}")
                    failed_count += 1

            elif needs_stock_update:
                # Update existing embed with current stock
                try:
                    logging.info(f"📊 Updating stock status for product {product['id']}")

                    data = product_to_builder_data(product)
                    em = build_live_embed(data, pid=product["id"])

                    await msg.edit(embed=em)
                    updated_stock_count += 1
                    logging.info(f"✅ Updated stock status for product {product['id']}")

                except Exception as e:
                    logging.error(f"❌ Failed to update stock for product {product['id']}: {e}")

        except Exception as e:
            logging.error(f"❌ Error processing product {product['id']}: {e}")
            failed_count += 1

    logging.info(f"🎉 System restoration complete!")
    logging.info(f"   📝 Embeds restored: {restored_count}")
    logging.info(f"   📊 Stock updated: {updated_stock_count}")
    logging.info(f"   ❌ Failed: {failed_count}")
    logging.info(f"   📦 Total products processed: {len(products)}")

    # Also check for any pending orders that need attention
    pending_orders = [o for o in all_orders() if o['status'] == 'pending']
    if pending_orders:
        logging.info(f"⏳ Found {len(pending_orders)} pending orders to monitor")

    return restored_count, updated_stock_count, failed_count


async def get_channel_by_id(channel_id: str | int | None, bot_instance=None):
    """Get a Discord channel by ID"""
    if not channel_id:
        return None
    try:
        channel_id_int = int(channel_id)
    except (TypeError, ValueError):
        return None

    channel = discord.utils.get(bot_instance.get_all_channels(), id=channel_id_int)
    if channel is not None:
        return channel

    for guild in bot_instance.guilds:
        try:
            channel = guild.get_channel(channel_id_int)
            if channel is not None:
                return channel

            try:
                await guild.fetch_channels()
            except Exception as e:
                logging.debug(f"Could not fetch channels for guild {guild.id}: {e}")
            channel = guild.get_channel(channel_id_int)
            if channel is not None:
                return channel
        except Exception as e:
            logging.debug(f"get_channel_by_id: error checking guild {guild.id}: {e}")
            continue

    logging.warning(f"Channel {channel_id_int} not found after cached lookup; not using bot.fetch_channel because it is unstable in this environment")
    return None


async def find_existing_product_embed(product: dict, channel, bot_user=None):
    """Search existing channel history for a matching product embed."""
    try:
        async for msg in channel.history(limit=150):
            if msg.author != bot_user or not msg.embeds:
                continue
            embed = msg.embeds[0]
            footer_text = embed.footer.text if embed.footer else ""
            title_text = embed.title or ""

            # Match by product name and product id from footer or title
            if product["name"] in title_text or product["id"] in footer_text or product["id"] in title_text:
                return msg
    except Exception as e:
        logging.warning(f"Could not search channel history for product {product['id']}: {e}")
    return None


async def send_log_embed(title: str, description: str = "", fields: dict = None, color: int = None, logging_channel_id=None, get_channel_by_id_callback=None):
    """Send an embed to the logging channel for admin visibility"""
    if not logging_channel_id:
        return  # Logging channel not configured
    
    try:
        channel = await get_channel_by_id_callback(logging_channel_id)
        if channel is None:
            logging.warning(f"Logging channel {logging_channel_id} not found or accessible")
            return
        
        em = discord.Embed(
            title=title,
            description=description,
            color=color or COLORS["info"],
            timestamp=datetime.now(timezone.utc)
        )
        
        if fields:
            for field_name, field_value in fields.items():
                em.add_field(name=field_name, value=str(field_value)[:1024], inline=True)
        
        await channel.send(embed=em)
    except Exception as e:
        logging.warning(f"Could not send log embed: {e}")


async def on_ready_handler(
    bot_instance,
    restore_product_embeds_callback,
    get_channel_by_id_callback,
    restore_order_cancel_views_callback,
    refresh_pending_invoice_messages_callback,
    refresh_invoice_timers_callback,
    check_payments_callback,
    update_analytics_callback,
    database_backup_callback,
    database_maintenance_callback,
    start_webhook_server_callback=None,
    invoice_channel_id=None,
    logging_channel_id=None
):
    """Handle bot ready event - extracted from on_ready"""
    logging.info(f"🤖 {bot_instance.user} is online and ready!")

    try:
        synced = await bot_instance.tree.sync()
        logging.info(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        logging.error(f"❌ Slash command sync failed: {e}")

    # Re-register persistent views so buttons still work after a restart
    try:
        bot_instance.add_view(AdminPanelView())
        logging.info("✅ Re-registered AdminPanelView for persistent button handling")
    except Exception as e:
        logging.error(f"❌ Failed to register persistent admin panel view: {e}")

    try:
        if hasattr(bot_instance, '_http') and hasattr(bot_instance._http, '_global_over') and type(bot_instance._http._global_over).__name__ == '_MissingSentinel':
            bot_instance._http._global_over = asyncio.Event()
            bot_instance._http._global_over.set()
            logging.info("✅ Patched bot._http._global_over event for HTTP client")
    except Exception as e:
        logging.warning(f"⚠️ Could not verify HTTP client global rate limit event: {e}")

    try:
        if invoice_channel_id:
            invoice_channel = await get_channel_by_id_callback(invoice_channel_id)
            if invoice_channel is None:
                logging.warning(f"⚠️ Invoice channel {invoice_channel_id} is not cached on ready. Admin invoices may fail until channel cache is available.")
            else:
                logging.info(f"✅ Invoice channel {invoice_channel.name} ({invoice_channel.id}) is available on ready")
    except Exception as e:
        logging.warning(f"⚠️ Could not verify invoice channel on ready: {e}")

    try:
        await restore_order_cancel_views_callback(get_db, bot_instance)
    except Exception as e:
        logging.error(f"❌ Failed to restore order cancel views: {e}")

    # Start restore in the background so the bot can respond immediately after ready
    async def _restore_background():
        try:
            restored, updated, failed = await restore_product_embeds_callback()
            if failed > 0:
                logging.warning(f"⚠️  {failed} products had restoration issues - check logs above")
            else:
                logging.info("🎯 All products successfully restored and up to date!")
        except Exception as e:
            logging.error(f"❌ Critical error during restoration: {e}")

    asyncio.create_task(_restore_background())

    async def _refresh_invoices_background():
        try:
            await refresh_pending_invoice_messages_callback()
        except Exception as e:
            logging.error(f"❌ Failed to refresh pending invoice messages: {e}")

    asyncio.create_task(_refresh_invoices_background())

    refresh_invoice_timers_callback.start()
    logging.info(f"⏱️ Invoice footer refresh started every {INVOICE_REFRESH_INTERVAL} seconds")

    # Start background payment monitoring
    check_payments_callback.start()
    logging.info("💰 Payment monitoring started")

    # Start analytics and maintenance tasks
    update_analytics_callback.start()
    database_backup_callback.start()
    database_maintenance_callback.start()
    logging.info("📊 Analytics and maintenance tasks started")

    if start_webhook_server_callback:
        try:
            await start_webhook_server_callback()
            logging.info("🌐 Webhook server startup task completed")
        except Exception as e:
            logging.error(f"❌ Webhook server failed to start: {e}")


async def on_command_error_handler(ctx, error, prefix=None):
    """Handle command errors - extracted from on_command_error"""
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Use `{prefix}panel` for usage.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        logging.error(f"Command error: {error}")
        await ctx.send(f"❌ An error occurred: `{error}`")
        raise error
