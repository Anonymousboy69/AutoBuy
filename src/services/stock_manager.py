# stock_manager.py - Stock management and UI updates
import logging
import asyncio
import discord
from datetime import datetime, timezone
from decimal import Decimal

from shopbot.database import get_db, get_product, get_order
from shopbot.shop import get_stock_status
from utils import DB_FILE, ADMIN_ROLE_ID, LOW_STOCK_THRESHOLD, COLORS
from ui.views import ProductDetailView
from src.http_utils import ensure_http_client_ready


async def update_stock_message(product_id: str, bot_instance):
    """Update the stock display message for a product"""
    product = get_product(DB_FILE, product_id)
    if not product:
        return
    channel_id = product.get("channel_id")
    embed_msg_id = product.get("embed_msg_id")
    if not channel_id or not embed_msg_id:
        return

    def clear_stale_embed():
        try:
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE products SET channel_id = NULL, embed_msg_id = NULL WHERE id = ?", (product_id,))
            conn.commit()
            conn.close()
            logging.info(f"Cleared stale embed reference for product {product_id}")
        except Exception as clear_error:
            logging.debug(f"Could not clear stale embed reference for product {product_id}: {clear_error}")

    try:
        channel = bot_instance.get_channel(int(channel_id))
        if channel is None:
            for guild in bot_instance.guilds:
                channel = guild.get_channel(int(channel_id))
                if channel:
                    break
        if channel is None:
            try:
                await ensure_http_client_ready(bot_instance)
                channel = await bot_instance.fetch_channel(int(channel_id))
            except Exception as e:
                logging.warning(f"⏳ Stock embed channel {channel_id} not in cache and fetch_channel failed: {e}")
                clear_stale_embed()
                return
        if channel is None:
            logging.warning(f"⏳ Stock embed channel {channel_id} not found")
            clear_stale_embed()
            return
        msg = await channel.fetch_message(int(embed_msg_id))
        if not msg.embeds:
            logging.warning(f"⏳ Stock embed message {embed_msg_id} for product {product_id} has no embeds")
            clear_stale_embed()
            return
        em = msg.embeds[0].copy()
        stock_count, stock_emoji = get_stock_status(DB_FILE, product_id)
        if stock_count == float('inf'):
            stock_val = "Unlimited"
        else:
            stock_val = f"{stock_count} in stock"

        current_stock_field = next((field for field in em.fields if "Stock" in field.name), None)
        if current_stock_field and current_stock_field.value == stock_val:
            return

        new_fields = []
        for field in em.fields:
            if "Stock" in field.name:
                new_fields.append({"name": field.name, "value": stock_val, "inline": field.inline})
            else:
                new_fields.append({"name": field.name, "value": field.value, "inline": field.inline})
        em.clear_fields()
        for f in new_fields:
            em.add_field(name=f["name"], value=f["value"], inline=f["inline"])

        async def edit_message():
            await msg.edit(embed=em, view=ProductDetailView(product_id))

        try:
            await edit_message()
        except Exception as e:
            if hasattr(e, 'status') and e.status == 429:
                retry_after = getattr(e, 'retry_after', 2)
                logging.warning(f"Discord rate limit hit updating stock embed for {product_id}, sleeping {retry_after}s")
                await asyncio.sleep(retry_after or 2)
                try:
                    await edit_message()
                except Exception as exc:
                    logging.exception(f"Could not update stock embed after retry for {product_id}")
            else:
                raise
    except Exception as e:
        logging.exception(f"Could not update stock embed for {product_id}")


async def send_low_stock_alert(product_id: str, bot_instance):
    """Send low stock alerts to admins"""
    product = get_product(DB_FILE, product_id)
    if not product or product["stock"] > LOW_STOCK_THRESHOLD:
        return
    for guild in bot_instance.guilds:
        admin_role = discord.utils.get(guild.roles, id=ADMIN_ROLE_ID)
        if not admin_role:
            continue
        em = discord.Embed(
            title=f"⚠️ Low Stock Alert",
            description=f"**{product['name']}** (`{product_id}`) is running low!",
            color=COLORS["warning"],
        )
        em.add_field(name="Stock Left", value=str(product["stock"]), inline=True)
        em.add_field(name="Restock Command", value=f"`/restock {product_id}`", inline=True)
        for member in admin_role.members:
            try:
                await member.send(embed=em)
            except Exception:
                pass


async def notify_next_in_queue(product_id: str, bot_instance, get_channel_callback, update_stock_callback, build_order_embed_callback):
    """Notify the next user(s) in queue when stock becomes available"""
    conn = get_db(DB_FILE)
    c = conn.cursor()

    while True:
        c.execute("""
            SELECT id, user_id, channel_id, message_id, quantity
            FROM orders
            WHERE product_id = ? AND status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
        """, (product_id,))
        queued_order = c.fetchone()

        if not queued_order:
            break

        order_id, user_id, channel_id, message_id, quantity = queued_order
        # No longer reserve stock - just notify user that stock is available
        # Stock will be assigned when payment is received (first pay wins)

        try:
            user = await bot_instance.fetch_user(int(user_id))
            channel = await get_channel_callback(channel_id)
            if channel is None:
                logging.warning(f"Could not resolve channel {channel_id} for queued order {order_id[:8]}")
                break

            now = datetime.now(timezone.utc).timestamp()
            c.execute("UPDATE orders SET status = 'pending', created_at = ? WHERE id = ?", (now, order_id))
            conn.commit()

            try:
                await update_stock_callback(product_id)
            except Exception:
                pass

            em = discord.Embed(
                title="⏰ Your Turn to Buy!",
                description=f"**{get_product(DB_FILE, product_id)['name']}** is available again!\n\n**⚠️ Please note:** Stock is NOT reserved for you. Other buyers can also purchase. **First-to-pay gets the item** so complete payment quickly!",
                color=COLORS["warning"]
            )
            em.add_field(
                name="Next Steps",
                value="⚠️ Your order is now active but stock is NOT reserved. Other buyers can also purchase. **First-to-pay gets the item** - check your DMs and complete payment quickly!",
                inline=False
            )
            await user.send(embed=em)

            order = get_order(order_id)
            product = get_product(DB_FILE, product_id)
            if order and product:
                await user.send(embed=build_order_embed_callback(order, product))

        except Exception as e:
            logging.warning(f"Could not notify queued user {user_id} for product {product_id}: {e}")
            break

    conn.close()