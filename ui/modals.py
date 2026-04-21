# ─────────────────────────────────────────────
#  DISCORD UI MODALS
# ─────────────────────────────────────────────
import discord
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import json
import uuid
import logging

from shopbot.database import get_db, get_product, log_audit, get_user_wallet, check_rate_limit, hash_content, check_duplicate_stock, set_user_wallet
from shopbot.crypto import fetch_ltc_usd_price, format_ltc, get_address_balance, litoshi_to_ltc
from shopbot.shop import get_stock_status, notify_next_in_queue, update_stock_message
from ui.embeds import build_live_embed, build_restock_embed, product_to_builder_data
from utils import RESTOCKING_STATUS, RESTOCK_RATE_LIMIT, DB_FILE, COLORS

URL_FIELDS = {"author_url", "thumbnail_url", "image_url", "footer_url"}
PARAGRAPH_FIELDS = {"description", "delivery"} | URL_FIELDS


def _get_bot_module():
    try:
        return importlib.import_module('src.bot')
    except ModuleNotFoundError:
        return importlib.import_module('bot')


class SingleFieldModal(discord.ui.Modal):
    def __init__(self, label: str, field_key: str, builder_view: "EmbedBuilderView",
                 is_int=False, is_float=False, placeholder=""):
        super().__init__(title=f"Edit {label}")
        self.field_key = field_key
        self.builder_view = builder_view
        self.is_int = is_int
        self.is_float = is_float
        self.value_input = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.paragraph if field_key in PARAGRAPH_FIELDS else discord.TextStyle.short,
            placeholder=placeholder or f"Enter {label.lower()}",
            required=False,
            max_length=500 if field_key in URL_FIELDS else 1024,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.value_input.value.strip()

        if self.field_key in URL_FIELDS and raw:
            if not raw.startswith(("http://", "https://")):
                await interaction.response.send_message(
                    "❌ Invalid URL. Must start with `http://` or `https://`.", ephemeral=True
                )
                return

        if self.is_float:
            try:
                raw = float(raw) if raw else None
            except ValueError:
                await interaction.response.send_message("❌ Invalid number.", ephemeral=True)
                return
        elif self.is_int:
            try:
                raw = int(raw) if raw else None
            except ValueError:
                await interaction.response.send_message("❌ Invalid integer.", ephemeral=True)
                return

        if self.field_key in URL_FIELDS:
            self.builder_view.data[self.field_key] = raw or ""
        else:
            self.builder_view.data[self.field_key] = raw

        await interaction.response.edit_message(
            embed=build_live_embed(self.builder_view.data),
            view=self.builder_view,
        )


class ColorModal(discord.ui.Modal, title="Set Embed Color"):
    color_input = discord.ui.TextInput(
        label="Hex color (e.g. #9B59B6)",
        placeholder="#9B59B6",
        max_length=9,
        required=False,
    )

    def __init__(self, builder_view: "EmbedBuilderView"):
        super().__init__()
        self.builder_view = builder_view

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color_input.value.strip().lstrip("#")
        try:
            self.builder_view.data["color"] = int(raw, 16) if raw else 0x9B59B6
        except ValueError:
            await interaction.response.send_message("❌ Invalid hex color.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_live_embed(self.builder_view.data),
            view=self.builder_view,
        )


class AddFieldModal(discord.ui.Modal, title="Add Embed Field"):
    field_name = discord.ui.TextInput(label="Field name", max_length=256)
    field_value = discord.ui.TextInput(label="Field value", style=discord.TextStyle.paragraph, max_length=1024)
    inline = discord.ui.TextInput(label="Inline? (yes/no)", max_length=3, required=False, placeholder="no")

    def __init__(self, builder_view: "EmbedBuilderView"):
        super().__init__()
        self.builder_view = builder_view

    async def on_submit(self, interaction: discord.Interaction):
        self.builder_view.data["fields"].append({
            "name": self.field_name.value.strip(),
            "value": self.field_value.value.strip(),
            "inline": self.inline.value.strip().lower() in {"yes", "y", "true"},
        })
        await interaction.response.edit_message(
            embed=build_live_embed(self.builder_view.data),
            view=self.builder_view,
        )


class ProductCreateModal(discord.ui.Modal, title="Product Details"):
    category_id_input = discord.ui.TextInput(
        label="Category ID",
        placeholder="e.g. gaming, services, digital",
        max_length=50,
        required=True,
    )
    price_input = discord.ui.TextInput(
        label="Price (USD)",
        placeholder="$10",
        max_length=20,
    )

    def __init__(self, builder_view: "EmbedBuilderView"):
        super().__init__()
        self.builder_view = builder_view

        if self.builder_view.data.get("price_usd") is not None:
            self.price_input.default = str(self.builder_view.data["price_usd"])
        if self.builder_view.data.get("category"):
            self.category_id_input.default = self.builder_view.data["category"]

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.error(f"[✗] ProductCreateModal.on_error: {error}")
        import traceback; traceback.print_exc()
        try:
            await interaction.followup.send(f"❌ Modal error: `{error}`", ephemeral=True)
        except Exception:
            try:
                await interaction.response.send_message(f"❌ Modal error: `{error}`", ephemeral=True)
            except Exception:
                pass

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        category_id = self.category_id_input.value.strip()
        if not category_id:
            await interaction.response.send_message(
                "❌ Category ID is required. Enter the Discord category channel ID.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command must be used inside a server, not in DMs.", ephemeral=True
            )
            return

        try:
            category_channel = guild.get_channel(int(category_id))
        except ValueError:
            category_channel = None

        if not isinstance(category_channel, discord.CategoryChannel):
            await interaction.response.send_message(
                "❌ Invalid category ID. Enter a valid Discord category channel ID from this server.",
                ephemeral=True,
            )
            return

        self.builder_view.data["category"] = category_id

        raw_price = self.price_input.value.strip()
        usd_amount = None
        if raw_price:
            try:
                usd_value = raw_price.lstrip("$").strip()
                usd_amount = float(usd_value.replace(",", ""))
            except ValueError:
                await interaction.response.send_message(
                    "❌ Invalid price. Enter a USD amount like `$10` or `10`.", ephemeral=True,
                )
                return
        elif self.builder_view.data.get("price_usd") is not None:
            usd_amount = float(self.builder_view.data["price_usd"])

        if usd_amount is not None:
            market_rate = await fetch_ltc_usd_price()
            if market_rate is None or market_rate <= 0:
                await interaction.response.send_message(
                    "❌ Could not fetch live LTC/USD market price. Try again later.", ephemeral=True
                )
                return
            price = round(usd_amount / market_rate, 8)
            self.builder_view.data["price_usd"] = usd_amount
            self.builder_view.data["price_ltc"] = price
        elif self.builder_view.data.get("price_ltc") is not None:
            price = self.builder_view.data["price_ltc"]
        else:
            await interaction.response.send_message(
                "❌ Set a price in USD or LTC before saving.", ephemeral=True,
            )
            return

        stock = self.builder_view.data.get("stock")
        if stock is None:
            stock = 0
        delivery = self.builder_view.data.get("delivery") or ""
        category = self.builder_view.data.get("category")

        if self.builder_view.edit_product_id:
            if self.builder_view.message:
                em_saving = discord.Embed(title="⏳ Saving…", description="Updating product embed, please wait...", color=bot_mod.COLORS["info"])
                for item in self.builder_view.children:
                    item.disabled = True
                try:
                    await self.builder_view.message.edit(embed=em_saving, view=self.builder_view)
                except Exception:
                    pass
            await interaction.response.defer(ephemeral=True)

            try:
                conn = bot_mod.get_db(bot_mod.DB_FILE)
                c = conn.cursor()
                
                # Get original product data to preserve channel_id and embed_msg_id
                c.execute('SELECT channel_id, embed_msg_id FROM products WHERE id = ?', (self.builder_view.edit_product_id,))
                original_product = c.fetchone()
                
                now = datetime.now(timezone.utc).timestamp()
                c.execute(
                    '''UPDATE products
                       SET name=?, description=?, delivery=?, stock=?, category=?,
                           price_ltc=?, price_usd=?, embed_data=?, updated_at=?,
                           channel_id=?, embed_msg_id=?
                       WHERE id=?''',
                    (
                        self.builder_view.data["title"] or f"Product-{uuid.uuid4().hex[:6]}",
                        self.builder_view.data.get("description", ""),
                        delivery,
                        stock,
                        category,
                        price,
                        self.builder_view.data.get("price_usd"),
                        json.dumps(self.builder_view.data),
                        now,
                        original_product['channel_id'] if original_product else None,
                        original_product['embed_msg_id'] if original_product else None,
                        self.builder_view.edit_product_id,
                    ),
                )
                conn.commit()
                conn.close()

                conn = bot_mod.get_db(bot_mod.DB_FILE)
                c = conn.cursor()
                c.execute('SELECT * FROM products WHERE id = ?', (self.builder_view.edit_product_id,))
                updated_row = c.fetchone()
                conn.close()
                
                if updated_row:
                    updated_product = dict(updated_row)
                else:
                    updated_product = {}

                refreshed, refresh_error = await bot_mod.refresh_product_embed(updated_product, interaction.guild)

                em_confirm = discord.Embed(title="✅ Product Updated", color=bot_mod.COLORS["success"])
                em_confirm.add_field(name="ID",    value=f"`{self.builder_view.edit_product_id}`", inline=True)
                em_confirm.add_field(name="Name",  value=self.builder_view.data["title"] or "Unnamed", inline=True)
                em_confirm.add_field(name="Price", value=f"{price} LTC", inline=True)
                if refreshed:
                    em_confirm.set_footer(text="Product updated successfully.")
                else:
                    em_confirm.set_footer(text="Product updated successfully. Note: Product embed could not be updated.")

            except Exception as e:
                logging.error(f"[✗] edit product save failed: {e}")
                em_confirm = discord.Embed(
                    title="❌ Save Failed",
                    description=f"`{e}`",
                    color=bot_mod.COLORS["error"],
                )

            if self.builder_view.message:
                try:
                    await interaction.edit_original_response(embed=em_confirm, view=None)
                    return
                except Exception as e:
                    logging.warning(f"[!] Could not edit original response: {e}")
                    try:
                        await self.builder_view.message.delete()
                    except Exception as e:
                        logging.warning(f"[!] Could not delete builder message: {e}")

            try:
                await interaction.followup.send(embed=em_confirm, ephemeral=True)
            except Exception as e:
                logging.warning(f"[!] Could not send save confirmation follow-up: {e}")
            return

        if self.builder_view.message:
            em_saving = discord.Embed(title="⏳ Saving…", color=bot_mod.COLORS["info"])
            for item in self.builder_view.children:
                item.disabled = True
            await interaction.response.edit_message(embed=em_saving, view=self.builder_view)
        else:
            await interaction.response.defer(ephemeral=True)

        try:
            product_id = await bot_mod.do_add_product(
                interaction,
                guild=guild,
                name=self.builder_view.data["title"] or f"Product-{uuid.uuid4().hex[:6]}",
                price_ltc=price,
                description=self.builder_view.data.get("description", ""),
                delivery=delivery,
                stock=stock,
                category=category,
                embed_data=self.builder_view.data,
                send_confirmation=False,
            )
            if not product_id:
                return

            em_confirm = discord.Embed(title="✅ Product Created", color=bot_mod.COLORS["success"])
            em_confirm.add_field(name="ID",    value=f"`{product_id}`", inline=True)
            em_confirm.add_field(name="Name",  value=self.builder_view.data["title"] or "Unnamed", inline=True)
            em_confirm.add_field(name="Price", value=f"{price} LTC", inline=True)
            em_confirm.set_footer(text=f"Use /restock {product_id} to add stock")

            if self.builder_view.message:
                try:
                    await interaction.edit_original_response(embed=em_confirm, view=None)
                    return
                except Exception as e:
                    logging.warning(f"[!] Could not edit original response: {e}")
                    try:
                        await self.builder_view.message.delete()
                    except Exception as e:
                        logging.warning(f"[!] Could not delete builder message: {e}")

            try:
                await interaction.followup.send(embed=em_confirm, ephemeral=True)
                return
            except Exception:
                pass
        except Exception as e:
            logging.error(f"[✗] do_add_product crashed: {e}")
            try:
                await interaction.followup.send(
                    f"❌ Something went wrong while creating the product: `{e}`\nCheck bot permissions (Manage Channels).",
                    ephemeral=True,
                )
            except Exception:
                pass

# ─────────────────────────────────────────────
#  CONFIRM CANCEL MODAL
class ConfirmCancelModal(discord.ui.Modal, title="Confirm Cancel"):
    confirm_text = discord.ui.TextInput(
        label="Type CONFIRM to cancel",
        style=discord.TextStyle.short,
        placeholder="CONFIRM",
        max_length=7,
    )

    def __init__(self, order_id: str):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm_text.value.strip().upper() != "CONFIRM":
            await interaction.response.send_message(
                "❌ You must type CONFIRM to cancel the order.",
                ephemeral=True,
            )
            return

        bot_mod = _get_bot_module()
        await bot_mod.do_cancel_order(interaction, self.order_id)


class RefundModal(discord.ui.Modal, title="Process Manual Refund"):
    refund_txid = discord.ui.TextInput(
        label="Refund Transaction ID (optional)",
        style=discord.TextStyle.short,
        placeholder="Leave empty if not yet processed",
        required=False,
        max_length=100,
    )
    
    refund_address = discord.ui.TextInput(
        label="Refund Address (where funds were sent)",
        style=discord.TextStyle.short,
        placeholder="LTC address where refund was sent",
        required=False,
        max_length=100,
    )

    def __init__(self, order_id: str, refundable_amount: Decimal):
        super().__init__()
        self.order_id = order_id
        self.refundable_amount = refundable_amount

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        order = bot_mod.get_order(self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        if order['status'] == 'refunded':
            await interaction.response.send_message("✅ This order has already been refunded.", ephemeral=True)
            return

        conn = bot_mod.get_db(bot_mod.DB_FILE)
        c = conn.cursor()
        now = datetime.now(timezone.utc).timestamp()

        refund_txid = self.refund_txid.value.strip() if self.refund_txid.value else None
        refund_address = self.refund_address.value.strip() if self.refund_address.value else None

        if refund_txid and refund_address:
            c.execute('''UPDATE orders SET status = ?, refund_txid = ?, refund_address = ?, refund_at = ? 
                         WHERE id = ?''',
                      ('refunded', refund_txid, refund_address, now, order['id']))
        else:
            c.execute('UPDATE orders SET status = ?, refund_at = ? WHERE id = ?',
                      ('refunded', now, order['id']))

        conn.commit()
        conn.close()
        
        try:
            user = await bot_mod.bot.fetch_user(int(order['user_id']))
            em = discord.Embed(
                title="💰 Manual Refund Processed",
                description=f"Your order **#{order['id'][:8]}** has been manually refunded by an admin.",
                color=bot_mod.COLORS["success"],
            )
            em.add_field(name="Refund Amount", value=f"{format_ltc(self.refundable_amount)} LTC", inline=True)
            if refund_txid:
                em.add_field(name="Refund TX ID", value=f"`{refund_txid[:16]}...`", inline=True)
            em.set_footer(text="Thank you for your patience")
            await user.send(embed=em)
        except Exception as e:
            logging.warning(f"Could not notify user {order['user_id']} about manual refund: {e}")
        
        try:
            await bot_mod.update_invoice_message(order, None)
        except Exception:
            pass
        
        product = get_product(DB_FILE, order['product_id'])
        log_audit(order['product_id'], "manual_refund_button", str(interaction.user.id), interaction.user.name, 
                  1, f"Order {order['id'][:8]} manually refunded via button (txid: {refund_txid or 'N/A'})")
        
        await interaction.response.send_message(
            f"✅ Order **#{order['id'][:8]}** marked as refunded.\n"
            f"User notified and invoice updated.",
            ephemeral=True
        )


class QuantityModal(discord.ui.Modal, title="Select Quantity"):
    quantity_input = discord.ui.TextInput(
        label="How many items do you want?",
        style=discord.TextStyle.short,
        placeholder="1",
        min_length=1,
        max_length=3,
    )
    
    confirm_text = discord.ui.TextInput(
        label="Type BUY to confirm purchase",
        style=discord.TextStyle.short,
        placeholder="BUY",
        max_length=4,
    )

    def __init__(self, product_id: str):
        super().__init__()
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        logging.info(f"QuantityModal submitted for product {self.product_id} user={interaction.user.id}")
        if self.confirm_text.value.strip().upper() != "BUY":
            await interaction.response.send_message(
                "❌ Please type BUY to confirm.", ephemeral=True
            )
            return
        try:
            quantity = int(self.quantity_input.value.strip())
            if quantity < 1:
                await interaction.response.send_message(
                    "❌ Quantity must be at least 1.", ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "❌ Please enter a valid number for quantity.", ephemeral=True
            )
            return

        product = get_product(DB_FILE, self.product_id)
        if not product:
            await interaction.response.send_message("❌ Product no longer exists.", ephemeral=True)
            return

        stock_count, _ = get_stock_status(DB_FILE, self.product_id)
        if stock_count != float('inf') and quantity > stock_count:
            embed = discord.Embed(
                title="⚠️ Stock Shortage",
                description="The requested quantity is not available right now.",
                color=COLORS["error"],
            )
            embed.add_field(name="Requested", value=str(quantity), inline=True)
            embed.add_field(name="Available", value=str(stock_count), inline=True)
            embed.set_footer(text="Try a smaller quantity or check again later.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception as e:
                logging.debug(f"QuantityModal defer failed: {e}")

        bot_mod = _get_bot_module()
        # Repair HTTP client before processing buy
        from src.http_utils import ensure_http_client_ready
        await ensure_http_client_ready(interaction.client)
        logging.info(f"Calling process_buy for product {self.product_id} with quantity {quantity}")
        await bot_mod.process_buy(interaction, self.product_id, quantity)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.error(f"[✗] QuantityModal.on_error: {error}")
        import traceback; traceback.print_exc()
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    "❌ Something went wrong while processing your order. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass
        else:
            try:
                await interaction.followup.send(
                    "❌ Something went wrong while processing your order. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass


class BuyProductModal(discord.ui.Modal, title="Buy Product"):
    product_id = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter the product ID from /shop",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        await bot_mod.process_buy(interaction, self.product_id.value.strip())


class OrderStatusModal(discord.ui.Modal, title="Check Order Status"):
    order_id = discord.ui.TextInput(
        label="Order ID",
        placeholder="Enter the first 8+ characters of your order ID",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        await bot_mod.send_order_status(interaction, self.order_id.value.strip())


class CheckStockModal(discord.ui.Modal, title="Check Product Stock"):
    product_id_input = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter product ID to check stock",
        max_length=50,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        product_id = self.product_id_input.value.strip()
        bot_mod = _get_bot_module()
        await bot_mod.handle_checkstock(interaction, product_id)


class CheckBalanceModal(discord.ui.Modal, title="Check Order Balance"):
    order_id_input = discord.ui.TextInput(
        label="Order ID (first 8 characters)",
        placeholder="Enter order ID to check LTC balance",
        max_length=20,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        order_id = self.order_id_input.value.strip()
        bot_mod = _get_bot_module()

        order = None
        for o in bot_mod.all_orders():
            if o['id'].startswith(order_id):
                order = o
                break

        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        try:
            balance = await get_address_balance(order['ltc_address'])
            if not balance:
                raise ValueError("No balance data returned from the LTC API")

            balance_ltc = litoshi_to_ltc(balance.get('balance', 0))
            price_usd = await fetch_ltc_usd_price()
            balance_usd = float(balance_ltc) * float(price_usd or 0)

            em = discord.Embed(
                title=f"⚖️ Order Balance Check",
                description=f"Order: `{order['id'][:8]}`",
                color=COLORS["info"]
            )

            em.add_field(name="💰 LTC Balance", value=f"**{balance_ltc:.8f} LTC**", inline=True)
            em.add_field(name="💵 USD Value", value=f"**${balance_usd:.2f}**", inline=True)
            em.add_field(name="📧 Address", value=f"`{order['ltc_address'][:16]}...`", inline=False)
            em.add_field(name="📦 Product", value=get_product(DB_FILE, order['product_id'])['name'] if get_product(DB_FILE, order['product_id']) else "Unknown", inline=True)
            em.add_field(name="👤 Customer", value=f"<@{order['user_id']}>", inline=True)

            await interaction.response.send_message(embed=em, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to check balance: {e}", ephemeral=True)


class EditProductModal(discord.ui.Modal, title="Edit Product"):
    product_id = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter the product ID",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        product_id = self.product_id.value.strip()
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        product = c.fetchone()
        conn.close()

        if not product:
            await interaction.response.send_message(f"❌ Product `{product_id}` not found.", ephemeral=True)
            return

        data = product_to_builder_data(dict(product))
        from ui.views import EmbedBuilderView

        view = EmbedBuilderView(interaction.user.id, edit_product_id=product_id)
        view.data = data

        await interaction.response.send_message(
            embed=build_live_embed(data, pid=product_id),
            view=view,
            ephemeral=True,
        )


class DeleteProductModal(discord.ui.Modal, title="Delete Product"):
    product_id = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter the product ID",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        await bot_mod.do_delete_product(interaction, self.product_id.value.strip())


class ResetConfirmModal(discord.ui.Modal, title="⚠️ CONFIRM DATABASE RESET"):
    confirmation = discord.ui.TextInput(
        label="Type 'RESET ALL DATA' to confirm",
        placeholder="This will delete EVERYTHING permanently",
        required=True,
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Import the owner check function
        from utils.constants import owner_check_interaction
        
        if not owner_check_interaction(interaction):
            await interaction.response.send_message("🚫 Owner only.", ephemeral=True)
            return

        if self.confirmation.value.strip() != "RESET ALL DATA":
            await interaction.response.send_message("❌ Incorrect confirmation text. Reset cancelled.", ephemeral=True)
            return

        bot_mod = _get_bot_module()
        await bot_mod.do_reset_database(interaction)


class RestockModal(discord.ui.Modal, title="Restock Single Item"):
    content = discord.ui.TextInput(
        label="Item Content",
        style=discord.TextStyle.paragraph,
        placeholder="Enter the stock item content here.",
        required=True,
        max_length=4000,
    )

    def __init__(self, product_id: str, message_id: int, channel_id: int):
        super().__init__()
        self.product_id = product_id
        self.message_id = message_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        # Ack quickly so followups are reliable
        await interaction.response.defer(ephemeral=True)
        from ui.views import RestockView
        wallet = get_user_wallet(DB_FILE, str(interaction.user.id))
        if not wallet:
            await interaction.followup.send(
                "❌ You must link your LTC wallet before restocking. Use `/wallet` or the wallet panel to link it.",
                ephemeral=True
            )
            return
        item_content = self.content.value.strip()
        if not item_content:
            await interaction.followup.send("❌ Item content cannot be empty.", ephemeral=True)
            return

        content_hash = hash_content(item_content)
        if check_duplicate_stock(DB_FILE, self.product_id, content_hash):
            await interaction.followup.send("⚠️ This item already exists for that product.", ephemeral=True)
            return

        item_id = uuid.uuid4().hex[:8]
        created_at = datetime.now(timezone.utc).timestamp()

        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute(
            '''INSERT INTO stock_items
               (id, product_id, content, status, content_hash, created_at, restocked_by, message_channel_id, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (item_id, self.product_id, item_content, RESTOCKING_STATUS, content_hash, created_at, str(interaction.user.id), self.channel_id, self.message_id),
        )
        conn.commit()
        conn.close()

        # Most reliable: turn the deferred response into the updated restock panel.
        # (Ephemeral messages can't be fetched by ID; followup delivery can be flaky depending on client/webhook state.)
        await interaction.edit_original_response(
            content=None,
            embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
            view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
        )
        try:
            from src.http_utils import ensure_http_client_ready
            channel = interaction.client.get_channel(self.channel_id)
            if channel is None:
                await ensure_http_client_ready(interaction.client)
                channel = await interaction.client.fetch_channel(self.channel_id)
            if channel is not None:
                message = await channel.fetch_message(self.message_id)
                product = get_product(DB_FILE, self.product_id)
                if product:
                    await message.edit(
                        embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                        view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                    )
        except Exception:
            pass


class BulkRestockModal(discord.ui.Modal, title="Bulk Restock Items"):
    content = discord.ui.TextInput(
        label="Item Contents",
        style=discord.TextStyle.paragraph,
        placeholder="Paste one stock item per line.",
        required=True,
        max_length=4000,
    )

    def __init__(self, product_id: str, message_id: int, channel_id: int):
        super().__init__()
        self.product_id = product_id
        self.message_id = message_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from ui.views import RestockView
        wallet = get_user_wallet(DB_FILE, str(interaction.user.id))
        if not wallet:
            await interaction.followup.send(
                "❌ You must link your LTC wallet before restocking. Use `/wallet` or the wallet panel to link it.",
                ephemeral=True
            )
            return
        content = self.content.value.strip()
        if not content:
            await interaction.followup.send("❌ No items provided.", ephemeral=True)
            return

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            await interaction.followup.send("❌ No valid items found.", ephemeral=True)
            return

        conn = get_db(DB_FILE)
        c = conn.cursor()
        added = 0
        skipped = 0
        now = datetime.now(timezone.utc).timestamp()

        for line in lines:
            content_hash = hash_content(line)
            if check_duplicate_stock(DB_FILE, self.product_id, content_hash):
                skipped += 1
                continue
            item_id = uuid.uuid4().hex[:8]
            c.execute(
                '''INSERT INTO stock_items
                   (id, product_id, content, status, content_hash, created_at, restocked_by, message_channel_id, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (item_id, self.product_id, line, RESTOCKING_STATUS, content_hash, now, str(interaction.user.id), self.channel_id, self.message_id),
            )
            added += 1

        conn.commit()
        conn.close()

        await interaction.edit_original_response(
            content=None,
            embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
            view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
        )
        try:
            from src.http_utils import ensure_http_client_ready
            channel = interaction.client.get_channel(self.channel_id)
            if channel is None:
                await ensure_http_client_ready(interaction.client)
                channel = await interaction.client.fetch_channel(self.channel_id)
            if channel is not None:
                message = await channel.fetch_message(self.message_id)
                product = get_product(DB_FILE, self.product_id)
                if product:
                    await message.edit(
                        embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                        view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                    )
        except Exception:
            pass


class RestockProductModal(discord.ui.Modal, title="Restock Product"):
    product_id = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter the product ID",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        product_id = self.product_id.value.strip()
        product = get_product(DB_FILE, product_id)
        if not product:
            await interaction.response.send_message(f"❌ Product `{product_id}` not found.", ephemeral=True)
            return

        from ui.views import RestockView

        view = RestockView(product_id, interaction.user, interaction.user.roles, interaction.guild)
        await interaction.response.send_message(
            embed=build_restock_embed(product_id, interaction.user, interaction.user.roles, interaction.guild),
            view=view,
            ephemeral=True,
        )


class AuditProductModal(discord.ui.Modal, title="Audit History"):
    product_id = discord.ui.TextInput(
        label="Product ID",
        placeholder="Enter the product ID",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot_mod = _get_bot_module()
        await bot_mod.send_audit_log(interaction, self.product_id.value.strip())


class SetWalletModal(discord.ui.Modal, title="Link LTC Wallet"):
    ltc_address = discord.ui.TextInput(
        label="LTC Address",
        placeholder="Enter your LTC address (ltc1, L, or M)",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        address = self.ltc_address.value.strip()
        if not address.startswith(('ltc1', 'LTC1', 'M', 'm', 'L', 'l')) or len(address) < 26:
            await interaction.response.send_message(
                "❌ Invalid LTC address format. LTC addresses start with 'ltc1', 'L/l', 'M/m', or '3'.",
                ephemeral=True
            )
            return

        if not set_user_wallet(DB_FILE, str(interaction.user.id), address, str(interaction.user.id)):
            await interaction.response.send_message(
                "❌ This LTC address is already linked to another user.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ **Wallet Linked Successfully!**\n\nLTC Address: `{address}`\n\nYou can now restock products and earn from sales!",
            ephemeral=True
        )


class EditItemModal(discord.ui.Modal, title="Edit Stock Item"):
    content_input = discord.ui.TextInput(
        label="Item Content",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        min_length=1,
        placeholder="Enter or modify the item content here...\n\n\n\n",
    )

    def __init__(self, product_id: str, item_id: str, current_content: str, original_message: discord.Message | None = None, page: int = 0):
        super().__init__()
        self.product_id = product_id
        self.item_id = item_id
        self.current_content = current_content
        self.original_message = original_message
        self.channel_id = original_message.channel.id if original_message else None
        self.message_id = original_message.id if original_message else None
        self.page = page
        self.content_input.default = current_content

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.content_input.value.strip()
        
        if not new_content:
            await interaction.response.send_message("❌ Content cannot be empty.", ephemeral=True)
            return

        # Defer immediately to acknowledge the interaction
        await interaction.response.defer()

        conn = get_db(DB_FILE)
        c = conn.cursor()
        
        # Check for duplicates
        new_hash = hash_content(new_content)
        c.execute('SELECT id FROM stock_items WHERE product_id = ? AND content_hash = ? AND id != ?',
                  (self.product_id, new_hash, self.item_id))
        if c.fetchone():
            await interaction.followup.send("⚠️ This content already exists.", ephemeral=True)
            conn.close()
            return
        
        # Update the item in database
        c.execute('UPDATE stock_items SET content = ?, content_hash = ? WHERE id = ?',
                  (new_content, new_hash, self.item_id))
        conn.commit()
        conn.close()
        logging.info(f"Database saved for item {self.item_id}: {new_content[:100]}")

        # Update the embed in real-time using the deferred ephemeral message
        try:
            em = self.original_message.embeds[0].copy()
            
            # Format new content
            if len(new_content) > 1900:
                display_content = new_content[:1897] + '...'
            else:
                display_content = new_content
            
            safe_display = display_content.replace('```', '`\u200b`')
            boxed = f"```\n{safe_display}\n```"
            
            # Preserve the original position of the Item Info field(s)
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

            # Add the updated content field(s) in the original position
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
                    em.add_field(name=f"Item Info ({idx}/{len(chunks)})", value=chunk, inline=False)
            else:
                em.add_field(name="Item Info", value=boxed, inline=False)

            for fname, fvalue, finline in other_fields[insertion_index:]:
                em.add_field(name=fname, value=fvalue, inline=finline)

            # Edit the deferred ephemeral message
            await interaction.edit_original_response(embed=em)
            logging.info(f"✅ Updated item {self.item_id} in ephemeral embed")
        except Exception as e:
            logging.warning(f"Failed to update ephemeral embed: {e}")
