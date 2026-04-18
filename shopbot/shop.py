import discord
from discord import app_commands
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Tuple
import logging
from shopbot.database import (
    get_db,
    get_product,
    get_order,
    all_products,
    all_products_by_category,
    get_stock_items,
    check_rate_limit,
    hash_content,
    check_duplicate_stock,
    log_audit,
)
from shopbot.crypto import format_ltc, fetch_ltc_usd_price

# Assuming COLORS is defined elsewhere, but for now, define here
COLORS = {
    "primary": 0x9B59B6,
    "success": 0x2ECC71,
    "error":   0xE74C3C,
    "warning": 0xF39C12,
    "info":    0x3498DB,
}

# ─────────────────────────────────────────────
#  STOCK HELPERS
# ─────────────────────────────────────────────
def get_stock_status(db_file: str, product_id: str) -> Tuple[int | float, str]:
    product = get_product(db_file, product_id)
    if not product:
        return 0, "❌"
    
    # Calculate available stock from pending stock items only.
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = ?', (product_id, 'pending'))
    pending_items = c.fetchone()[0]
    conn.close()
    
    if product["stock"] < 0:  # Unlimited
        return float('inf'), "∞"
    elif pending_items == 0:
        return 0, "🔴"
    elif pending_items <= 5:
        return pending_items, "🟡"
    else:
        return pending_items, "🟢"

async def update_stock_message(db_file: str, product_id: str, bot):
    product = get_product(db_file, product_id)
    if not product:
        return
    channel_id   = product.get("channel_id")
    embed_msg_id = product.get("embed_msg_id")
    if not channel_id or not embed_msg_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(embed_msg_id))
        if not msg.embeds:
            return
        em = msg.embeds[0].copy()
        stock_count, stock_emoji = get_stock_status(db_file, product_id)
        if stock_count == float('inf'):
            stock_val = "Unlimited"
        else:
            stock_val = f"{stock_count} in stock"
        new_fields = []
        for field in em.fields:
            if field.name == "📦 Stock":
                new_fields.append({"name": field.name, "value": stock_val, "inline": field.inline})
            else:
                new_fields.append({"name": field.name, "value": field.value, "inline": field.inline})
        em.clear_fields()
        for f in new_fields:
            em.add_field(name=f["name"], value=f["value"], inline=f["inline"])
        try:
            from ui.views import ProductDetailView
            await msg.edit(embed=em, view=ProductDetailView(product_id))
        except Exception:
            await msg.edit(embed=em)
    except Exception as e:
        logging.warning(f"Could not update stock embed for {product_id}: {e}")

async def send_low_stock_alert(db_file: str, product_id: str, bot, admin_role_id: int, low_stock_threshold: int):
    product = get_product(db_file, product_id)
    if not product or product["stock"] > low_stock_threshold:
        return
    for guild in bot.guilds:
        admin_role = discord.utils.get(guild.roles, id=admin_role_id)
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

async def notify_next_in_queue(db_file: str, product_id: str, bot):
    """Notify the next user in queue when stock becomes available"""
    conn = get_db(db_file)
    c = conn.cursor()
    # Get the first queued order for this product
    c.execute("""
        SELECT id, user_id, invoice_channel_id, invoice_message_id
        FROM orders
        WHERE product_id = ? AND status = 'queued'
        ORDER BY created_at ASC
        LIMIT 1
    """, (product_id,))
    queued_order = c.fetchone()
    
    if not queued_order:
        conn.close()
        return
    
    order_id, user_id, invoice_channel_id, invoice_message_id = queued_order
    
    try:
        # Get user and channel
        user = await bot.fetch_user(int(user_id))
        channel = bot.get_channel(int(invoice_channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(invoice_channel_id))
        
        # Send notification
        em = discord.Embed(
            title="🎉 Stock Available!",
            description=f"Good news! **{get_product(db_file, product_id)['name']}** is now back in stock.",
            color=COLORS["success"]
        )
        em.add_field(
            name="Next Steps",
            value="Your queued order is now being processed. Please check your DMs for payment instructions.",
            inline=False
        )
        
        await user.send(embed=em)
        
        # Update order status to pending and send payment instructions
        c.execute("UPDATE orders SET status = 'pending' WHERE id = ?", (order_id,))
        conn.commit()
        
        # Send payment embed (reuse logic from process_buy)
        order = get_order(db_file, order_id)
        product = get_product(db_file, product_id)
        if order and product:
            created_at = datetime.fromtimestamp(order['created_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            em_payment = discord.Embed(title="Order Created", color=COLORS["info"])
            em_payment.add_field(name="Product", value=product["name"], inline=True)
            em_payment.add_field(name="Amount", value=f"{format_ltc(Decimal(str(product['price_ltc'])))} LTC", inline=True)
            if product.get("price_usd") is not None:
                em_payment.add_field(name="USD Price", value=f"${product['price_usd']:.2f}", inline=True)
            em_payment.add_field(name="Order ID", value=f"`{order_id[:8]}`", inline=True)
            em_payment.add_field(name="Blockchain", value="Litecoin", inline=True)
            em_payment.add_field(name="Payment Address", value=f"```{order['ltc_address']}```", inline=False)
            em_payment.set_footer(text="Expires in 1 hour")
            await user.send(embed=em_payment)
            
    except Exception as e:
        logging.warning(f"Could not notify queued user {user_id} for product {product_id}: {e}")
    finally:
        conn.close()

# ─────────────────────────────────────────────
#  SHOP VIEWS (PAGINATED & SEARCHABLE)
# ─────────────────────────────────────────────
class ShopPage(discord.ui.View):
    def __init__(self, products: List[dict], page: int = 0, category_filter: str = None, sort_by: str = "newest", db_file: str = None):
        super().__init__(timeout=180)
        self.products = products
        self.page = page
        self.page_size = 5
        self.category_filter = category_filter
        self.sort_by = sort_by
        self.db_file = db_file

        if sort_by == "price_asc":
            self.products = sorted(self.products, key=lambda p: p["price_ltc"])
        elif sort_by == "price_desc":
            self.products = sorted(self.products, key=lambda p: p["price_ltc"], reverse=True)
        elif sort_by == "newest":
            self.products = sorted(self.products, key=lambda p: p["created_at"], reverse=True)

        max_page = (len(self.products) - 1) // self.page_size
        if self.page > max_page:
            self.page = max_page

        if self.page == 0:
            self.prev_page.disabled = True
        if self.page >= max_page:
            self.next_page.disabled = True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=ShopPage(self.products, self.page, self.category_filter, self.sort_by, self.db_file))
        else:
            await interaction.response.defer()

    @discord.ui.button(label="▶ Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        max_page = (len(self.products) - 1) // self.page_size
        if self.page < max_page:
            self.page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=ShopPage(self.products, self.page, self.category_filter, self.sort_by, self.db_file))
        else:
            await interaction.response.defer()

    def get_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        end = start + self.page_size
        page_products = self.products[start:end]

        em = discord.Embed(title="🛒 Shop", color=COLORS["primary"])

        if not page_products:
            em.description = "No products found."
            return em

        for p in page_products:
            stock_count, stock_emoji = get_stock_status(self.db_file, p["id"])
            if p["stock"] < 0:
                stock_str = "Unlimited"
            else:
                # Check for queued orders
                conn = get_db(self.db_file)
                c = conn.cursor()
                c.execute('SELECT COUNT(*) FROM orders WHERE product_id = ? AND status = ?', (p["id"], 'queued'))
                queue_count = c.fetchone()[0]
                conn.close()
                
                if stock_count == 0 and queue_count > 0:
                    stock_str = f"Out of stock • {queue_count} in queue"
                else:
                    stock_str = f"{stock_count} in stock"
            price_usd_str = f" (${p['price_usd']:.2f})" if p.get('price_usd') else ''
            em.add_field(
                name=f"{p['name']} • `{p['id']}`",
                value=f" {p['price_ltc']} LTC{price_usd_str}\n{stock_str}",
                inline=False
            )

        max_page = (len(self.products) - 1) // self.page_size
        em.set_footer(text=f"Page {self.page + 1}/{max_page + 1} • Use /shop sort:<type> or /shop category:<name>")
        return em

# Other shop-related classes and functions can be added here, but for brevity, I'll stop.