# ─────────────────────────────────────────────
#  DISCORD UI VIEWS
# ─────────────────────────────────────────────
import discord
from discord import ui
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple
import logging
import asyncio
import importlib

# Import from modules
from shopbot.database import get_db, get_order, assign_order_stock_to_order, build_seller_payout_from_pending_stock, log_audit, get_product, check_rate_limit, hash_content, check_duplicate_stock, get_seller_revenue, record_payout, get_payout_history, remove_user_wallet
from shopbot.shop import get_stock_status, update_stock_message, notify_next_in_queue
from shopbot.crypto import find_address_path_by_address, sweep_payment, litoshi_to_ltc, format_ltc, get_address_balance

# Import from utils
from utils import (
    CONFIG, DB_FILE, COLORS, STATUS_EMOJI, PAYMENT_TIMEOUT, POLL_INTERVAL,
    LTC_CONFIRMATIONS, RESTOCK_RATE_LIMIT, LOW_STOCK_THRESHOLD, MAX_SWEEP_ATTEMPTS,
    SWEEP_RETRY_BACKOFF, MAX_REFUND_ATTEMPTS, RESTOCKING_STATUS,
    get_expiration_footer, get_expiration_timestamp, mask_wallet_address, format_usd,
    user_has_admin_or_seller_role, admin_check_interaction, seller_check_interaction,
    owner_or_admin_check_interaction,
    admin_or_seller_check_interaction, is_admin, RECEIVING_ADDRESS, WALLET_SEED,
    get_next_blockcypher_token
)

from ui.embeds import (
    build_invoice_embed,
    build_live_embed,
    default_embed_data,
    build_restock_embed,
    build_no_stock_embed,
    build_seller_wallet_embed,
    get_visible_stock_items,
)
from ui.modals import (
    RefundModal,
    ConfirmCancelModal,
    SingleFieldModal,
    ColorModal,
    AddFieldModal,
    ProductCreateModal,
    QuantityModal,
    BuyProductModal,
    OrderStatusModal,
    CheckStockModal,
    CheckBalanceModal,
    EditProductModal,
    DeleteProductModal,
    RestockProductModal,
    RestockModal,
    BulkRestockModal,
    AuditProductModal,
    EditItemModal,
    SetWalletModal,
)


def _get_bot_module():
    try:
        return importlib.import_module('src.bot')
    except ModuleNotFoundError:
        return importlib.import_module('bot')


def get_user_wallet(user_id: str) -> Optional[dict]:
    bot_mod = _get_bot_module()
    return bot_mod.get_user_wallet(user_id)


async def update_invoice_message(order: dict, balance_info: dict | None = None):
    bot_mod = _get_bot_module()
    return await bot_mod.update_invoice_message(order, balance_info)


async def deliver_order(order: dict, oid: str, force_delivery: bool = False):
    bot_mod = _get_bot_module()
    return await bot_mod.deliver_order(order, oid, force_delivery)


async def get_address_transactions(address: str) -> list:
    bot_mod = _get_bot_module()
    return await bot_mod.get_address_transactions(address)


async def fetch_wallet_panel_data(user_id: str):
    bot_mod = _get_bot_module()
    return await bot_mod.fetch_wallet_panel_data(user_id)


class PartialPaymentConfirmView(discord.ui.View):
    def __init__(self, order_id: str, confirmed_amount: Decimal, balance_info: dict):
        super().__init__(timeout=300)
        self.order_id = order_id
        self.confirmed_amount = confirmed_amount
        self.balance_info = balance_info

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm Partial Payment", style=discord.ButtonStyle.success, custom_id="confirm_partial")
    async def confirm_partial(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        order = get_order(DB_FILE, self.order_id)
        if not order:
            await interaction.followup.send("❌ Order not found.", ephemeral=True)
            return

        if order['status'] == 'delivered':
            await interaction.followup.send("✅ This order has already been delivered.", ephemeral=True)
            return

        quantity = int(order.get('quantity', 1))
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = "pending"',
                  (order['product_id'],))
        available_stock = c.fetchone()[0]
        conn.close()

        if available_stock < quantity:
            await interaction.followup.send(
                f"❌ **Cannot sweep:** Order requires {quantity} item(s) but only {available_stock} pending item(s) available. Stock must be available BEFORE sweeping.",
                ephemeral=True,
            )
            return

        address_path = order.get("address_path")
        if not address_path:
            address_path = find_address_path_by_address(DB_FILE, order['ltc_address'], WALLET_SEED)
            if address_path:
                conn = get_db(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE orders SET address_path = ? WHERE id = ?', (address_path, self.order_id))
                conn.commit()
                conn.close()

        if not address_path:
            await interaction.followup.send("❌ Could not derive wallet path. Contact developer.", ephemeral=True)
            return

        recipients, error = build_seller_payout_from_pending_stock(DB_FILE, order, CONFIG['shop'].get('platform_fee_percent', 0.0), RECEIVING_ADDRESS)
        if error:
            logging.warning(f"Seller direct payout unavailable: {error}")
            recipients = None

        swept, sweep_txid = await sweep_payment(
            DB_FILE,
            address_path,
            order['ltc_address'],
            self.confirmed_amount,
            WALLET_SEED,
            RECEIVING_ADDRESS,
            get_next_blockcypher_token(),
            LTC_CONFIRMATIONS,
            recipients=None,  # ⚠️ Don't pay sellers on partial payments - accumulate funds instead
        )

        if not swept:
            await interaction.followup.send("❌ Sweep failed. Please try again or check logs.", ephemeral=True)
            return

        assigned = assign_order_stock_to_order(DB_FILE, order)
        if not assigned:
            logging.warning(f"Could not assign stock to order {order['id']} for partial payment payout.")
            await interaction.followup.send(
                f"⚠️ Sweep succeeded but stock assignment failed. Expected {quantity} items, only {available_stock} available.",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc).timestamp()
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('''UPDATE orders SET status = ?, paid_at = ?, swept_at = ?, sweep_txid = ?, sweep_attempts = sweep_attempts + 1, last_sweep_attempt = ?
                     WHERE id = ?''',
                  ('paid', now, now, sweep_txid, now, self.order_id))
        conn.commit()
        conn.close()

        order['status'] = 'paid'
        order['paid_at'] = now
        order['swept_at'] = now
        order['sweep_txid'] = sweep_txid
        await update_invoice_message(order, self.balance_info)
        await deliver_order(order, self.order_id, force_delivery=True)

        await interaction.followup.send(f"✅ Accepted {format_ltc(self.confirmed_amount)} LTC partial payment. Order delivered.", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, custom_id="cancel_partial")
    async def cancel_partial(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Partial payment approval cancelled.", ephemeral=True)


class InvoiceApproveView(discord.ui.View):
    def __init__(self, order_id: str, disabled: bool = False, show_refund: bool = False, show_sweep: bool = False):
        super().__init__(timeout=None)
        self.order_id = order_id
        
        # Remove buttons that shouldn't be shown (completely hidden, not disabled)
        if not show_refund:
            self.remove_item(self.refund)
        
        if not show_sweep:
            self.remove_item(self.sweep_and_deliver)
        
        # Disable all buttons if the view itself is disabled
        if disabled:
            self.approve.disabled = True
            if show_refund and self.refund in self.children:
                self.refund.disabled = True
            if show_sweep and self.sweep_and_deliver in self.children:
                self.sweep_and_deliver.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Approve Order", style=discord.ButtonStyle.success, custom_id="invoice_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = get_order(DB_FILE, self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        if order['status'] == 'delivered':
            await interaction.response.send_message("✅ This order has already been delivered.", ephemeral=True)
            return

        if order['status'] in {'canceled', 'expired', 'refunded', 'failed'}:
            await interaction.response.send_message(
                "❌ This order cannot be approved because it is already canceled, expired, refunded, or failed.",
                ephemeral=True,
            )
            return

        quantity = int(order.get('quantity', 1))
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = "pending"',
                  (order['product_id'],))
        available_stock = c.fetchone()[0]
        conn.close()

        if available_stock < quantity:
            await interaction.response.send_message(
                f"❌ **Cannot approve:** Order requires {quantity} item(s) but only {available_stock} pending item(s) available in stock.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        balance_info = await get_address_balance(order['ltc_address'])
        expected = Decimal(str(order['price_ltc'])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        confirmed = litoshi_to_ltc(balance_info.get('balance', 0)) if balance_info else Decimal('0')
        unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0)) if balance_info else Decimal('0')

        if confirmed < expected and unconfirmed < expected:
            await interaction.followup.send(
                f"⚠️ Payment is not sufficient ({confirmed} LTC received, {expected} LTC expected), but **forcing delivery** with available stock.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("✅ Order approved. Delivery is now processing.", ephemeral=True)

        if not assign_order_stock_to_order(DB_FILE, order):
            await interaction.followup.send(
                f"❌ **Stock assignment failed** - Could not allocate {quantity} item(s) from available {available_stock} pending stock.",
                ephemeral=True,
            )
            return

        try:
            product = get_product(DB_FILE, order['product_id'])
            await interaction.message.edit(
                embed=build_invoice_embed(order, product, {'balance': 0, 'unconfirmed_balance': 0}, processing=True),
                view=InvoiceApproveView(self.order_id, disabled=True, show_refund=False),
            )
        except Exception:
            pass

        async def background_approve():
            try:
                await deliver_order(order, self.order_id, force_delivery=True)
            except Exception as exc:
                logging.error(f"[background_approve] delivery failed for {self.order_id}: {exc}")
            try:
                updated_order = get_order(DB_FILE, self.order_id)
                await update_invoice_message(updated_order, await get_address_balance(order['ltc_address']))
            except Exception:
                pass

        asyncio.create_task(background_approve())

    @discord.ui.button(label="💰 Process Refund", style=discord.ButtonStyle.secondary, custom_id="invoice_refund")
    async def refund(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = get_order(DB_FILE, self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        if order['status'] == 'refunded':
            await interaction.response.send_message("✅ This order has already been refunded.", ephemeral=True)
            return

        balance_info = await get_address_balance(order['ltc_address'])
        confirmed = litoshi_to_ltc(balance_info.get('balance', 0)) if balance_info else Decimal('0')
        unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0)) if balance_info else Decimal('0')
        refundable_amount = confirmed if confirmed > 0 else unconfirmed
        
        if refundable_amount <= 0:
            await interaction.followup.send(
                "❌ No balance found on this address to refund.",
                ephemeral=True,
            )
            return

        modal = RefundModal(self.order_id, refundable_amount)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="💰 Sweep & Deliver", style=discord.ButtonStyle.secondary, custom_id="invoice_sweep_deliver")
    async def sweep_and_deliver(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            order = get_order(DB_FILE, self.order_id)
            if not order:
                await interaction.response.send_message("❌ Order not found.", ephemeral=True)
                return

            if order['status'] == 'delivered':
                await interaction.response.send_message("✅ This order has already been delivered.", ephemeral=True)
                return

            quantity = int(order.get('quantity', 1))
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = "pending"',
                      (order['product_id'],))
            available_stock = c.fetchone()[0]
            conn.close()

            if available_stock < quantity:
                await interaction.response.send_message(
                    f"❌ **Cannot sweep:** Order requires {quantity} item(s) but only {available_stock} pending item(s) available. Stock must be available BEFORE sweeping.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            
            try:
                balance_info = await asyncio.wait_for(get_address_balance(order['ltc_address']), timeout=10)
            except asyncio.TimeoutError:
                await interaction.followup.send("❌ Balance check timed out. Try again in a moment.", ephemeral=True)
                return
            except Exception as e:
                logging.error(f"Balance fetch error for sweep: {e}")
                await interaction.followup.send(f"❌ Could not fetch balance: {str(e)[:100]}", ephemeral=True)
                return

            if not balance_info:
                await interaction.followup.send("❌ Could not fetch balance. Try again later.", ephemeral=True)
                return

            confirmed = litoshi_to_ltc(balance_info.get("balance", 0))
            expected = Decimal(str(order['price_ltc'])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

            if confirmed <= 0:
                await interaction.followup.send("❌ No confirmed payment found on this address.", ephemeral=True)
                return

            if confirmed < expected:
                em = discord.Embed(
                    title="💰 Partial Payment Detected",
                    description=f"This order requires {format_ltc(expected)} LTC but only {format_ltc(confirmed)} LTC is confirmed.",
                    color=COLORS['warning']
                )
                em.add_field(name="Shortfall", value=f"{format_ltc(expected - confirmed)} LTC", inline=True)
                em.add_field(name="Action", value="Click 'Confirm' to accept partial payment and deliver.", inline=False)
                view = PartialPaymentConfirmView(self.order_id, confirmed, balance_info)
                await interaction.followup.send(embed=em, view=view, ephemeral=True)
                return

            address_path = order.get("address_path")
            if not address_path:
                try:
                    address_path = find_address_path_by_address(DB_FILE, order['ltc_address'], WALLET_SEED)
                    if address_path:
                        conn = get_db(DB_FILE)
                        c = conn.cursor()
                        c.execute('UPDATE orders SET address_path = ? WHERE id = ?', (address_path, self.order_id))
                        conn.commit()
                        conn.close()
                except Exception as e:
                    logging.warning(f"Could not find address path: {e}")
                    address_path = None

            sweep_txid = None
            if address_path:
                try:
                    recipients, error = build_seller_payout_from_pending_stock(DB_FILE, order, CONFIG['shop'].get('platform_fee_percent', 0.0), RECEIVING_ADDRESS)
                    if error:
                        logging.warning(f"Seller direct payout unavailable: {error}")
                        recipients = None

                    swept, sweep_txid = await asyncio.wait_for(
                        sweep_payment(
                            DB_FILE,
                            address_path,
                            order['ltc_address'],
                            confirmed,
                            WALLET_SEED,
                            RECEIVING_ADDRESS,
                            get_next_blockcypher_token(),
                            LTC_CONFIRMATIONS,
                            recipients=recipients,
                        ),
                        timeout=30
                    )
                    if not swept:
                        await interaction.followup.send("❌ Sweep failed. Please try again or check logs.", ephemeral=True)
                        return

                    assigned = assign_order_stock_to_order(DB_FILE, order)
                    if not assigned:
                        logging.warning(f"Could not assign stock to order {order['id']} for payout.")
                except asyncio.TimeoutError:
                    await interaction.followup.send("❌ Sweep operation timed out. Please try again.", ephemeral=True)
                    return
                except Exception as e:
                    logging.error(f"Sweep payment error: {e}", exc_info=True)
                    await interaction.followup.send(f"❌ Sweep failed: {str(e)[:100]}", ephemeral=True)
                    return
            else:
                logging.warning(f"No address path for order {self.order_id}, skipping sweep")

            now = datetime.now(timezone.utc).timestamp()
            conn = get_db(DB_FILE)
            c = conn.cursor()
            c.execute('''UPDATE orders SET status = ?, paid_at = ?, swept_at = ?, sweep_txid = ?, sweep_attempts = sweep_attempts + 1, last_sweep_attempt = ?
                         WHERE id = ?''',
                      ('paid', now, now, sweep_txid, now, self.order_id))
            conn.commit()
            conn.close()

            order['status'] = 'paid'
            order['paid_at'] = now
            order['swept_at'] = now
            order['sweep_txid'] = sweep_txid
            
            try:
                await update_invoice_message(order, balance_info)
            except Exception as e:
                logging.warning(f"Could not update invoice: {e}")
            
            try:
                await deliver_order(order, self.order_id)
            except Exception as e:
                logging.error(f"Delivery error: {e}", exc_info=True)
                await interaction.followup.send(f"⚠️ Order marked paid but delivery had an error: {str(e)[:100]}", ephemeral=True)
                return

            await interaction.followup.send("✅ Funds swept and order delivered.", ephemeral=True)
            
        except Exception as e:
            logging.error(f"Sweep & Deliver handler error: {e}", exc_info=True)
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(f"❌ Unexpected error: {str(e)[:100]}", ephemeral=True)
                except Exception:
                    pass
            else:
                try:
                    await interaction.followup.send(f"❌ Unexpected error: {str(e)[:100]}", ephemeral=True)
                except Exception:
                    pass


class OrderCancelView(discord.ui.View):
    def __init__(self, order_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.order_id = order_id
        if disabled:
            self.cancel_order.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Don't send response in check - let the button handler do it
        return True

    @discord.ui.button(label="❌ Cancel Order", style=discord.ButtonStyle.danger, custom_id="cancel_order")
    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            order = get_order(DB_FILE, self.order_id)
            if not order:
                await interaction.response.send_message("❌ Order not found.", ephemeral=True)
                return

            if str(interaction.user.id) != order['user_id']:
                await interaction.response.send_message("🚫 You can only cancel your own order.", ephemeral=True)
                return

            if order['status'] in {'paid', 'delivered', 'failed', 'expired', 'sweep_failed', 'canceled', 'refunded'}:
                await interaction.response.send_message(
                    "❌ This order cannot be canceled at this stage.",
                    ephemeral=True,
                )
                return

            try:
                if interaction.response.is_done():
                    try:
                        await interaction.followup.send(
                            "⚠️ Unable to open the cancellation confirmation because the interaction was already acknowledged. Please try again.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

                await interaction.response.send_modal(ConfirmCancelModal(self.order_id))
            except discord.NotFound:
                logging.warning(f"Cancel order interaction expired for {self.order_id}")
            except discord.HTTPException as e:
                logging.warning(f"Cancel order modal open failed for {self.order_id}: {e}")
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(
                            "⚠️ Unable to open cancellation confirmation right now. Please try again in a moment.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logging.error(f"Unexpected error opening cancel modal for {self.order_id}: {e}", exc_info=True)
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(
                            "⚠️ An unexpected error occurred. Please try again.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
        except discord.NotFound:
            logging.error(f"Interaction token expired for order {self.order_id}")
        except Exception as e:
            logging.error(f"Unexpected error in cancel_order button: {e}", exc_info=True)

    @discord.ui.button(label="📱 Show QR", style=discord.ButtonStyle.blurple, custom_id="order_show_qr")
    async def show_qr(self, interaction: discord.Interaction, button: discord.ui.Button):
        from urllib.parse import quote
        order = get_order(DB_FILE, self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        if not order.get('ltc_address'):
            await interaction.response.send_message("❌ No payment address for this order.", ephemeral=True)
            return

        from shopbot.crypto import format_ltc
        from decimal import Decimal
        total_price_ltc = Decimal(str(order.get('price_ltc', '0')))
        if total_price_ltc <= 0:
            await interaction.response.send_message("❌ Invalid order amount.", ephemeral=True)
            return

        # Generate QR from plain address (working format)
        qr_data = quote(order['ltc_address'])
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&ecc=M&data={qr_data}"

        # Create embed with QR code
        em = discord.Embed(
            title="💳 Payment QR Code",
            description=f"Scan to send {format_ltc(total_price_ltc)} LTC",
            color=COLORS["info"]
        )
        em.set_image(url=qr_url)
        em.add_field(name="Address", value=f"```{order['ltc_address']}```", inline=False)
        em.add_field(name="Amount to Send", value=f"**{format_ltc(total_price_ltc)} LTC**", inline=True)

        await interaction.response.send_message(embed=em, ephemeral=True)


class EmbedBuilderView(discord.ui.View):
    def __init__(self, owner_id: int, edit_product_id: str = None):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.edit_product_id = edit_product_id
        self.data = default_embed_data()
        self.message = None

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        if isinstance(error, discord.NotFound) or (
            isinstance(error, discord.HTTPException) and getattr(error, 'code', None) == 10062
        ):
            logging.warning(f"[!] EmbedBuilderView interaction expired for {item}: {error}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "⚠️ This interaction expired or is no longer valid. Please reopen the builder and try again.",
                        ephemeral=True,
                    )
            except Exception:
                pass
            return

        logging.error(f"[✗] EmbedBuilderView button error ({item}): {error}")
        import traceback; traceback.print_exc()
        try:
            await interaction.followup.send(f"❌ Button error: `{error}`", ephemeral=True)
        except Exception:
            try:
                await interaction.response.send_message(f"❌ Button error: `{error}`", ephemeral=True)
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🚫 Only the admin who opened this builder may use it.", ephemeral=True
            )
            return False
        return True

    async def safe_send_modal(self, interaction: discord.Interaction, modal: discord.ui.Modal):
        try:
            await interaction.response.send_modal(modal)
        except discord.NotFound as e:
            logging.warning(f"[!] Modal send failed: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Unable to open the modal because the interaction is no longer valid. Please reopen the builder and try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
        except discord.HTTPException as e:
            logging.error(f"[!] HTTP error sending modal: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Unable to open the modal right now. Please try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"[!] Unexpected error sending modal: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Something went wrong when opening the modal.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

    @discord.ui.button(label="Title", style=discord.ButtonStyle.primary, row=0)
    async def btn_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(
            interaction,
            SingleFieldModal("Title", "title", self, placeholder="Product title"),
        )

    @discord.ui.button(label="Description", style=discord.ButtonStyle.primary, row=0)
    async def btn_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Description", "description", self))

    @discord.ui.button(label="Author Name", style=discord.ButtonStyle.primary, row=0)
    async def btn_author_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Author Name", "author_name", self))

    @discord.ui.button(label="Author URL", style=discord.ButtonStyle.primary, row=0)
    async def btn_author_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Author URL", "author_url", self))

    @discord.ui.button(label="Thumbnail", style=discord.ButtonStyle.primary, row=0)
    async def btn_thumbnail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Thumbnail URL", "thumbnail_url", self))

    @discord.ui.button(label="Image URL", style=discord.ButtonStyle.primary, row=1)
    async def btn_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Image URL", "image_url", self))

    @discord.ui.button(label="Footer Text", style=discord.ButtonStyle.primary, row=1)
    async def btn_footer_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Footer Text", "footer_text", self))

    @discord.ui.button(label="Footer URL", style=discord.ButtonStyle.primary, row=1)
    async def btn_footer_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, SingleFieldModal("Footer URL", "footer_url", self))

    @discord.ui.button(label="Color", style=discord.ButtonStyle.primary, row=1)
    async def btn_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, ColorModal(self))

    @discord.ui.button(label="Add Field", style=discord.ButtonStyle.primary, row=1)
    async def btn_add_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_send_modal(interaction, AddFieldModal(self))

    @discord.ui.button(label="Remove Field", style=discord.ButtonStyle.danger, row=3)
    async def btn_remove_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.data["fields"]:
            await interaction.response.send_message("No custom fields to remove.", ephemeral=True)
            return
        self.data["fields"].pop()
        await interaction.response.edit_message(
            embed=build_live_embed(self.data), view=self,
        )

    @discord.ui.button(label="Save Embed", style=discord.ButtonStyle.success, row=3)
    async def btn_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        await self.safe_send_modal(interaction, ProductCreateModal(self))

    @discord.ui.button(label="Reset Embed", style=discord.ButtonStyle.danger, row=3)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.data = default_embed_data()
        await interaction.response.edit_message(
            embed=build_live_embed(self.data), view=self,
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=3)
    async def btn_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Product creation canceled.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)


def start_product_builder(interaction: discord.Interaction) -> EmbedBuilderView:
    return EmbedBuilderView(interaction.user.id)


class ShopPage(discord.ui.View):
    def __init__(self, products: List[dict], page: int = 0, category_filter: str = None, sort_by: str = "newest"):
        super().__init__(timeout=180)
        self.products = products
        self.page = page
        self.page_size = 5
        self.category_filter = category_filter
        self.sort_by = sort_by

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
            await interaction.response.edit_message(embed=self.get_embed(), view=ShopPage(self.products, self.page, self.category_filter, self.sort_by))
        else:
            await interaction.response.defer()

    @discord.ui.button(label="▶ Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        max_page = (len(self.products) - 1) // self.page_size
        if self.page < max_page:
            self.page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=ShopPage(self.products, self.page, self.category_filter, self.sort_by))
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
            stock_count, stock_emoji = get_stock_status(DB_FILE, p["id"])
            if p["stock"] < 0:
                stock_str = "Unlimited"
            else:
                # Check for queued orders
                conn = get_db(DB_FILE)
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


class StockItemPage(discord.ui.View):
    def __init__(self, product_id: str, stock_items: List[dict], page: int = 0):
        super().__init__(timeout=None)
        self.product_id = product_id
        self.stock_items = stock_items
        self.page = page
        self.product = get_product(DB_FILE, product_id)

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= len(self.stock_items) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if owner_or_admin_check_interaction(interaction):
            return True
        seller_id = str(interaction.user.id)
        if any(str(item.get('restocked_by', '')).strip() != seller_id for item in self.stock_items):
            await interaction.response.send_message(
                "🚫 You can only view your own restocked stock items.", ephemeral=True
            )
            return False
        return True

    def normalize_page(self) -> bool:
        if not self.stock_items:
            self.page = 0
            return False
        self.page = max(0, min(self.page, len(self.stock_items) - 1))
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.normalize_page():
            await interaction.response.send_message("No stock items available.", ephemeral=True)
            return
        if self.page > 0:
            self.page -= 1
        await self.refresh_view(interaction)

    @discord.ui.button(label="▶ Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.normalize_page():
            await interaction.response.send_message("No stock items available.", ephemeral=True)
            return
        if self.page < len(self.stock_items) - 1:
            self.page += 1
        await self.refresh_view(interaction)

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        from ui.modals import EditItemModal

        if not self.normalize_page():
            await interaction.response.send_message("No stock items available.", ephemeral=True)
            return

        current_item = self.stock_items[self.page]
        await interaction.response.send_modal(
            EditItemModal(
                self.product_id,
                current_item['id'],
                current_item.get('content', ''),
                interaction.message,
                self.page,
            )
        )

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=0)
    async def delete_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.normalize_page():
            await interaction.response.send_message("No stock items available.", ephemeral=True)
            return

        current_item = self.stock_items[self.page]
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM stock_items WHERE id = ? AND product_id = ?', (current_item['id'], self.product_id))
        c.execute('''UPDATE products
                     SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending')
                     WHERE id = ?''', (self.product_id, self.product_id))
        conn.commit()
        conn.close()

        await update_stock_message(DB_FILE, self.product_id, interaction.client)

        self.stock_items.pop(self.page)
        if self.page >= len(self.stock_items) and self.page > 0:
            self.page -= 1

        if not self.stock_items:
            em = build_no_stock_embed(self.product_id)
            try:
                await interaction.response.edit_message(embed=em, view=None)
            except (discord.errors.NotFound, discord.errors.HTTPException):
                if interaction.message:
                    try:
                        await interaction.message.edit(embed=em, view=None)
                    except Exception as exc2:
                        logging.warning(f"StockItemPage delete direct edit failed: {exc2}")
            except Exception as exc:
                logging.warning(f"StockItemPage delete fallback failed: {exc}")
            return

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= len(self.stock_items) - 1
        try:
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        except (discord.errors.NotFound, discord.errors.HTTPException):
            if interaction.message:
                try:
                    await interaction.message.edit(embed=self.get_embed(), view=self)
                except Exception as exc2:
                    logging.warning(f"StockItemPage delete direct edit failed: {exc2}")
        except Exception as exc:
            logging.warning(f"StockItemPage delete fallback failed: {exc}")

    def get_embed(self) -> discord.Embed:
        item = self.stock_items[self.page]
        title = f"📦 Available Stock Item {self.page + 1}/{len(self.stock_items)}"
        if self.product:
            title = f"{title} — {self.product['name']}"

        stock_count, stock_emoji = get_stock_status(DB_FILE, self.product_id)
        if stock_count == float('inf'):
            stock_text = "Unlimited"
        else:
            stock_text = f"{stock_count} in stock"

        em = discord.Embed(title=title, color=COLORS['primary'])
        em.add_field(name='Item ID', value=f"`{item.get('id', 'N/A')}`", inline=True)
        em.add_field(name='Product ID', value=f"`{self.product_id}`", inline=True)
        em.add_field(name='Status', value=item.get('status', 'unknown').capitalize(), inline=True)

        if item.get('restocked_by'):
            restocked_by = str(item['restocked_by']).strip()
            if restocked_by.isdigit():
                em.add_field(name='Restocked By', value=f"<@{restocked_by}> ({restocked_by})", inline=False)
                seller_wallet = get_user_wallet(restocked_by)
                if seller_wallet and seller_wallet.get('ltc_address'):
                    em.add_field(name='Seller LTC Address', value=f"`{seller_wallet['ltc_address']}`", inline=False)
            else:
                em.add_field(name='Restocked By', value=restocked_by, inline=False)
        if item.get('delivered_to'):
            em.add_field(name='Delivered To', value=item['delivered_to'], inline=True)
        if item.get('delivered_at'):
            delivered_at = datetime.fromtimestamp(item['delivered_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            em.add_field(name='Delivered At', value=delivered_at, inline=True)
        if item.get('created_at'):
            created_at = datetime.fromtimestamp(item['created_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            em.add_field(name='Created', value=created_at, inline=True)

        content = item.get('content', 'No content available')
        if len(content) > 1000:
            content = content[:997] + '...'
        em.add_field(name='Content', value=f"```{content}```", inline=False)

        em.set_footer(text=f"Item {self.page + 1} of {len(self.stock_items)}")
        return em

    async def refresh_view(self, interaction: discord.Interaction):
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= len(self.stock_items) - 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)


class EmptyStockItemPage(discord.ui.View):
    def __init__(self, product_id: str, product: dict):
        super().__init__(timeout=None)
        self.product_id = product_id
        self.product = product
        self.prev_page.disabled = True
        self.next_page.disabled = True
        self.edit_item.disabled = True
        self.delete_item.disabled = True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("No stock items available.", ephemeral=True)

    @discord.ui.button(label="▶ Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("No stock items available.", ephemeral=True)

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("No stock items available.", ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=0)
    async def delete_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("No stock items available.", ephemeral=True)


class ProductDetailView(discord.ui.View):
    def __init__(self, product_id: str):
        super().__init__(timeout=None)
        self.product_id = product_id

        self.buy_button = discord.ui.Button(
            label="🛒 Buy Now - Click Here",
            style=discord.ButtonStyle.success,
            custom_id=f"product_buy_button:{product_id}",
        )
        self.buy_button.callback = self._buy_button_callback
        self.add_item(self.buy_button)

        stock_count, _ = get_stock_status(DB_FILE, self.product_id)
        if stock_count == 0:
            self.buy_button.disabled = True
            self.buy_button.label = "❌ Out of stock"
            self.buy_button.style = discord.ButtonStyle.danger

    async def safe_send_modal(self, interaction: discord.Interaction, modal: discord.ui.Modal):
        try:
            await interaction.response.send_modal(modal)
        except discord.NotFound as e:
            logging.warning(f"[!] Modal send failed: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Unable to open the modal because the interaction is no longer valid. Please refresh and try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
        except discord.HTTPException as e:
            logging.error(f"[!] HTTP error sending modal: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Unable to open the modal right now. Please try again.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"[!] Unexpected error sending modal: {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "⚠️ Something went wrong when opening the modal.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

    async def _buy_button_callback(self, interaction: discord.Interaction):
        logging.info(f"Product buy button clicked for product={self.product_id} user={interaction.user.id}")
        try:
            # Repair HTTP client before any channel fetches
            from src.http_utils import ensure_http_client_ready
            await ensure_http_client_ready(interaction.client)
            logging.info(f"Immediately opening QuantityModal for product {self.product_id}")
            await self.safe_send_modal(interaction, QuantityModal(self.product_id))
        except Exception as e:
            logging.error(f"Unexpected error in buy_button for product {self.product_id}: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ An unexpected error occurred. Please try again.", ephemeral=True)
            except Exception:
                pass



class RestockView(discord.ui.View):
    def __init__(self, product_id: str, user: discord.User | None = None, roles: List[discord.Role] | None = None, guild: discord.Guild | None = None):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.user = user
        self.roles = roles
        self.guild = guild

    @discord.ui.button(label="Add Single", style=discord.ButtonStyle.primary, custom_id="restock_add_single")
    async def add_single(self, interaction: discord.Interaction, button: discord.ui.Button):
        wallet = get_user_wallet(str(interaction.user.id))
        if not wallet:
            await interaction.response.send_message(
                "❌ You must link your LTC wallet before restocking. Use `/wallet` or the wallet panel to link it.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(RestockModal(self.product_id, interaction.message.id, interaction.channel.id))

    @discord.ui.button(label="Add Multiple", style=discord.ButtonStyle.primary, custom_id="restock_add_multiple")
    async def add_multiple(self, interaction: discord.Interaction, button: discord.ui.Button):
        wallet = get_user_wallet(str(interaction.user.id))
        if not wallet:
            await interaction.response.send_message(
                "❌ You must link your LTC wallet before restocking. Use `/wallet` or the wallet panel to link it.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(BulkRestockModal(self.product_id, interaction.message.id, interaction.channel.id))

    @discord.ui.button(label="View Staging", style=discord.ButtonStyle.secondary, custom_id="restock_view_staging")
    async def view_count(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = get_visible_stock_items(self.product_id, interaction.user, interaction.user.roles, interaction.guild, RESTOCKING_STATUS)
        product = get_product(DB_FILE, self.product_id)

        if not product:
            await interaction.response.send_message("❌ Product not found.", ephemeral=True)
            return

        if not items:
            await interaction.response.send_message("✅ No staged items.", ephemeral=True)
            return

        view = PaginatedStockView(self.product_id, items, product)
        try:
            await interaction.response.edit_message(embed=view.get_embed(), view=view)
        except Exception as exc:
            logging.warning(f"View Staging edit failed: {exc}")
            try:
                await interaction.message.edit(embed=view.get_embed(), view=view)
            except Exception as exc2:
                logging.warning(f"View Staging fallback edit failed: {exc2}")
                await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="✅ Done Restocking", style=discord.ButtonStyle.success, custom_id="restock_done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db(DB_FILE)
        c = conn.cursor()
        if owner_or_admin_check_interaction(interaction):
            c.execute("UPDATE stock_items SET status = ? WHERE product_id = ? AND status = ?", ("pending", self.product_id, RESTOCKING_STATUS))
        else:
            seller_id = str(interaction.user.id)
            c.execute("UPDATE stock_items SET status = ? WHERE product_id = ? AND status = ? AND restocked_by = ?", ("pending", self.product_id, RESTOCKING_STATUS, seller_id))
        moved_items = c.rowcount
        if moved_items > 0:
            c.execute("""UPDATE products SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending') WHERE stock >= 0 AND id = ?""", (self.product_id, self.product_id))
        conn.commit()
        conn.close()

        # Update the public product embed in the shop channel (if it exists)
        await update_stock_message(DB_FILE, self.product_id, interaction.client)

        total_stock, _ = get_stock_status(DB_FILE, self.product_id)
        product = get_product(DB_FILE, self.product_id)
        product_name = product["name"] if product and product.get("name") else "this product"

        published_count = moved_items
        item_word = "item" if published_count == 1 else "items"
        done_embed = discord.Embed(
            title="✅ Items Restocked",
            description=(
                f"{published_count} staged {item_word} published to **{product_name}**.\n\n"
                "Stock availability has been updated."
            ),
            color=COLORS["success"],
        )

        try:
            await interaction.response.edit_message(embed=done_embed, view=None)
        except Exception:
            try:
                await interaction.message.edit(embed=done_embed, view=None)
            except Exception:
                await interaction.response.send_message(embed=done_embed, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="restock_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel restocking session and delete staged items."""
        conn = get_db(DB_FILE)
        c = conn.cursor()
        if owner_or_admin_check_interaction(interaction):
            c.execute(
                "DELETE FROM stock_items WHERE product_id = ? AND status = ?",
                (self.product_id, RESTOCKING_STATUS),
            )
        else:
            seller_id = str(interaction.user.id)
            c.execute(
                "DELETE FROM stock_items WHERE product_id = ? AND status = ? AND restocked_by = ?",
                (self.product_id, RESTOCKING_STATUS, seller_id),
            )
        deleted = c.rowcount
        conn.commit()
        conn.close()

        product = get_product(DB_FILE, self.product_id)
        product_name = product["name"] if product and product.get("name") else "Unknown Product"
        item_word = "item" if deleted == 1 else "items"

        cancel_embed = discord.Embed(
            title="❌ Restock Session Cancelled",
            description=f"Cancelled restock for **{product_name}** and deleted **{deleted}** staged item(s).",
            color=COLORS["error"],
        )

        try:
            await interaction.response.edit_message(embed=cancel_embed, view=None)
        except Exception:
            try:
                await interaction.message.edit(embed=cancel_embed, view=None)
            except Exception:
                await interaction.response.send_message(embed=cancel_embed, ephemeral=True)


class PaginatedStockView(discord.ui.View):
    def __init__(self, product_id: str, items: list, product: dict, page: int = 0):
        super().__init__(timeout=600)
        self.product_id = product_id
        self.items = items
        self.product = product
        self.page = page
        self.items_per_page = 1
        self.total_pages = len(items)
        self.update_buttons()

    def update_buttons(self):
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.total_pages - 1

    def get_embed(self) -> discord.Embed:
        current_item = self.items[self.page]
        item_content = current_item.get('content', '') or ''
        if len(item_content) > 1900:
            item_content = item_content[:1897] + '...'

        safe_content = item_content.replace('```', '`\u200b`')
        if safe_content:
            boxed = (
                "```\n"
                f"{safe_content}\n"
                "```"
            )
        else:
            boxed = (
                "```\n"
                "No item content available.\n"
                "```"
            )

        em = discord.Embed(
            title=f"Restock Item {self.page + 1}/{self.total_pages}",
            color=COLORS["primary"],
        )

        em.add_field(name="Product ID", value=f"`{self.product.get('id', self.product_id)}`", inline=True)
        em.add_field(name="Item ID", value=f"`{current_item['id']}`", inline=True)

        if len(boxed) > 1024:
            chunks = []
            current_chunk = "```\n"
            for line in safe_content.split('\n'):
                if len(current_chunk) + len(line) + 5 > 900:
                    chunks.append(current_chunk + "\n```")
                    current_chunk = "```\n" + line
                else:
                    current_chunk += line + "\n"
            if current_chunk != "```\n":
                chunks.append(current_chunk + "\n```")

            for idx, chunk in enumerate(chunks, 1):
                field_name = f"Item Info ({idx}/{len(chunks)})" if len(chunks) > 1 else "Item Info"
                em.add_field(name=field_name, value=chunk, inline=False)
        else:
            em.add_field(name="Item Info", value=boxed, inline=False)

        em.set_footer(text=f"Page {self.page + 1} / {self.total_pages}")
        return em

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Manage ✏️", style=discord.ButtonStyle.primary, row=0)
    async def manage_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.items:
            await interaction.response.send_message("✅ No items to manage.", ephemeral=True)
            return

        current_item = self.items[self.page]
        item_content = current_item['content'] or ''
        if len(item_content) > 4096:
            item_content = item_content[:4090] + "..."

        safe_content = item_content.replace('```', '`\u200b`')
        boxed = (
            "```\n"
            f"{safe_content}\n"
            "```"
        )

        em = discord.Embed(title="Manage Item", color=COLORS["primary"])

        em.add_field(name="Item ID", value=f"`{current_item['id']}`", inline=True)
        em.add_field(name="Product ID", value=f"`{self.product_id}`", inline=True)

        if len(boxed) > 1024:
            chunks = []
            current_chunk = "```\n"
            for line in safe_content.split('\n'):
                if len(current_chunk) + len(line) + 5 > 900:
                    chunks.append(current_chunk + "\n```")
                    current_chunk = "```\n" + line
                else:
                    current_chunk += line + "\n"
            if current_chunk != "```\n":
                chunks.append(current_chunk + "\n```")

            for idx, chunk in enumerate(chunks, 1):
                field_name = f"Item Info ({idx}/{len(chunks)})" if len(chunks) > 1 else "Item Info"
                em.add_field(name=field_name, value=chunk, inline=False)
        else:
            em.add_field(name="Item Info", value=boxed, inline=False)

        em.set_footer(text="Choose an action")

        manage_view = ManageItemView(
            self.product_id,
            current_item['id'],
            item_content,
            self.items,
            self.product,
            self.page,
        )
        await interaction.response.edit_message(embed=em, view=manage_view)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back_to_manager(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(
                embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
            )
        except Exception as exc:
            logging.warning(f"Back to manager edit failed: {exc}")
            try:
                await interaction.message.edit(
                    embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                    view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                )
            except Exception as exc2:
                logging.warning(f"Back to manager fallback edit failed: {exc2}")
                await interaction.response.send_message(
                    embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                    view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                    ephemeral=True,
                )


class ManageItemView(discord.ui.View):
    def __init__(self, product_id: str, item_id: str, item_content: str, items: list, product: dict, page: int):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.item_id = item_id
        self.item_content = item_content
        self.items = items
        self.product = product
        self.page = page

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Reload latest content from DB so the modal always shows current data
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT content FROM stock_items WHERE id = ? AND product_id = ?', (self.item_id, self.product_id))
        row = c.fetchone()
        conn.close()
        latest_content = row[0] if row else self.item_content
        await interaction.response.send_modal(EditItemModal(self.product_id, self.item_id, latest_content, interaction.message, self.page))

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=0)
    async def delete_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM stock_items WHERE id = ? AND product_id = ?', (self.item_id, self.product_id))
        c.execute('''UPDATE products
                     SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending')
                     WHERE id = ?''', (self.product_id, self.product_id))
        conn.commit()
        conn.close()

        items = get_visible_stock_items(self.product_id, interaction.user, interaction.user.roles, interaction.guild, RESTOCKING_STATUS)
        if items:
            page = min(self.page, len(items) - 1)
            page_view = PaginatedStockView(self.product_id, items, self.product, page)
            await interaction.response.edit_message(embed=page_view.get_embed(), view=page_view)
        else:
            await interaction.response.edit_message(
                embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
            )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=0)
    async def back_to_staging(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = get_visible_stock_items(self.product_id, interaction.user, interaction.user.roles, interaction.guild, RESTOCKING_STATUS)
        page_view = PaginatedStockView(self.product_id, items, self.product, self.page)
        await interaction.response.edit_message(embed=page_view.get_embed(), view=page_view)


class RestockPageView(discord.ui.View):
    def __init__(self, product_id: str, items: list[dict], user_name: str):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.items = items
        self.user_name = user_name
        self.page = 0
        self.total_pages = max(1, len(items))
        self.update_buttons()

    def update_buttons(self):
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.total_pages - 1

    def get_embed(self) -> discord.Embed:
        current_item = self.items[self.page]
        item_content = current_item.get('content', '') or ''
        if len(item_content) > 2048:
            description = item_content[:1995] + '...'
        else:
            description = item_content

        safe_content = description.replace('```', '`\u200b`')
        boxed = (
            "```\n"
            f"{safe_content}\n"
            "```"
        )

        em = discord.Embed(
            title=f"Restock Item {self.page + 1}/{self.total_pages}",
            color=COLORS["success"],
        )

        if len(boxed) > 1024:
            chunks = []
            current_chunk = "```\n"
            for line in safe_content.split('\n'):
                if len(current_chunk) + len(line) + 5 > 900:
                    chunks.append(current_chunk + "\n```")
                    current_chunk = "```\n" + line
                else:
                    current_chunk += line + "\n"
            if current_chunk != "```\n":
                chunks.append(current_chunk + "\n```")

            for idx, chunk in enumerate(chunks, 1):
                field_name = f"Item Info ({idx}/{len(chunks)})" if len(chunks) > 1 else "Item Info"
                em.add_field(name=field_name, value=chunk, inline=False)
        else:
            em.add_field(name="Item Info", value=boxed, inline=False)

        em.add_field(name="Product ID", value=f"`{self.product_id}`", inline=True)
        em.add_field(name="Item ID", value=f"`{current_item['id']}`", inline=True)
        em.set_footer(text=f"Restocked by {self.user_name} — Page {self.page + 1}/{self.total_pages}")
        return em

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()


class ItemActionView(discord.ui.View):
    def __init__(self, product_id: str, item_id: str, item_content: str):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.item_id = item_id
        self.item_content = item_content

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
    async def edit_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT content FROM stock_items WHERE id = ? AND product_id = ?', (self.item_id, self.product_id))
        row = c.fetchone()
        conn.close()
        latest_content = row[0] if row else self.item_content
        await interaction.response.send_modal(EditItemModal(self.product_id, self.item_id, latest_content, interaction.message))

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM stock_items WHERE id = ? AND product_id = ?', (self.item_id, self.product_id))

        # Recalculate stock count
        c.execute('''UPDATE products
                     SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending')
                     WHERE id = ?''', (self.product_id, self.product_id))
        conn.commit()
        conn.close()

        product = get_product(DB_FILE, self.product_id)
        items = get_visible_stock_items(self.product_id, interaction.user, interaction.user.roles, interaction.guild, RESTOCKING_STATUS)
        if items:
            page_view = PaginatedStockView(self.product_id, items, product)
            await interaction.response.edit_message(embed=page_view.get_embed(), view=page_view)
        else:
            await interaction.response.edit_message(
                embed=build_restock_embed(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
                view=RestockView(self.product_id, interaction.user, interaction.user.roles, interaction.guild),
            )


class RestockTriggerView(discord.ui.View):
    def __init__(self, product_id: str, owner_id: int):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🚫 Only the command author can use this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📦 Add Stock Item", style=discord.ButtonStyle.success)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(RestockModal(self.product_id, interaction.message.id, interaction.channel.id))
        except Exception as e:
            logging.error(f"[✗] RestockTriggerView.open_modal failed: {e}")
            try:
                await interaction.response.send_message(
                    "❌ Could not open restock modal.", ephemeral=True
                )
            except Exception:
                pass


class WalletView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="Set Wallet", style=discord.ButtonStyle.success, emoji="💰", row=0, custom_id="wallet_set")
    async def set_wallet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return
        await interaction.response.send_modal(SetWalletModal())

    @discord.ui.button(label="Earnings", style=discord.ButtonStyle.primary, emoji="📊", row=0, custom_id="wallet_earnings")
    async def earnings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        revenue_data = get_seller_revenue(self.user_id, platform_fee_percent=CONFIG['shop'].get('platform_fee_percent', 0.0))

        em = discord.Embed(title="📊 Earnings Breakdown", color=COLORS["success"])
        if revenue_data:
            em.add_field(name="Total Revenue", value=f"**{format_ltc(Decimal(str(revenue_data['total_revenue'])))} LTC**", inline=False)
            em.add_field(name="Total Orders", value=str(revenue_data.get('total_orders', 0)), inline=True)
            em.add_field(name="Unique Buyers", value=str(revenue_data.get('unique_buyers', 0)), inline=True)

        await interaction.followup.send(embed=em, ephemeral=True)

    @discord.ui.button(label="History", style=discord.ButtonStyle.primary, emoji="📜", row=0, custom_id="wallet_history")
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        wallet = get_user_wallet(str(interaction.user.id))

        if not wallet:
            await interaction.followup.send("❌ No wallet linked.", ephemeral=True)
            return

        try:
            transactions = await asyncio.wait_for(
                get_address_transactions(wallet['ltc_address']),
                timeout=2.0
            )

            em = discord.Embed(title="📜 Transaction History", color=COLORS["info"])
            if transactions:
                tx_lines = []
                for tx in transactions[:5]:
                    amount = litoshi_to_ltc(tx.get('value', 0))
                    confirmed = "✓" if tx.get('confirmations', 0) > 0 else "◯"
                    date = tx.get('confirmed', 'unknown')[:10]
                    tx_lines.append(f"{confirmed} {format_ltc(amount)} LTC • {date}")
                em.description = "\n".join(tx_lines)
            else:
                em.description = "No transactions yet"
        except Exception as e:
            em = discord.Embed(title="❌ Error", description=f"Could not fetch history: {str(e)[:100]}", color=COLORS["error"])

        await interaction.followup.send(embed=em, ephemeral=True)

    @discord.ui.button(label="Payouts", style=discord.ButtonStyle.primary, emoji="💵", row=0, custom_id="wallet_payouts")
    async def payouts(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        all_payouts = get_payout_history(self.user_id, limit=10)

        em = discord.Embed(title="💵 Payout History", color=COLORS["success"])
        if all_payouts:
            payout_lines = []
            for payout in all_payouts[:5]:
                amount = format_ltc(Decimal(str(payout.get('amount_ltc', 0))))
                status = "✓ Completed" if payout.get('status') == 'completed' else "⏳ Pending"
                date = datetime.fromtimestamp(payout.get('created_at', 0), timezone.utc).strftime('%Y-%m-%d')
                payout_lines.append(f"{status} • {amount} LTC • {date}")
            em.description = "\n".join(payout_lines)
        else:
            em.description = "No payouts yet"

        await interaction.followup.send(embed=em, ephemeral=True)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="🗑️", row=1, custom_id="wallet_remove")
    async def remove_wallet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return

        wallet = get_user_wallet(str(interaction.user.id))
        if not wallet:
            await interaction.response.send_message("⚠️ No wallet is currently linked.", ephemeral=True)
            return

        remove_user_wallet(str(interaction.user.id))

        em = build_seller_wallet_embed(self.user_id)
        await interaction.response.edit_message(embed=em, view=self)
        await interaction.followup.send("✅ Wallet removed successfully.", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1, custom_id="wallet_refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return

        await interaction.response.defer()

        balance_info, revenue_data, payout_history, all_payouts, price_usd, recent_transactions = await fetch_wallet_panel_data(self.user_id)

        em = build_seller_wallet_embed(
            self.user_id,
            balance_info=balance_info,
            revenue_data=revenue_data,
            payout_history=payout_history,
            all_payouts=all_payouts,
            price_usd=price_usd,
            recent_transactions=recent_transactions,
        )

        await interaction.edit_original_response(embed=em, view=self)

class ProductSelect(discord.ui.Select):
    def __init__(self, products: dict):
        options = []
        for p in products[:25]:
            stock_count, stock_emoji = get_stock_status(DB_FILE, p["id"])
            if p["stock"] < 0:
                stock_str = "Unlimited"
            else:
                stock_str = f"{stock_count} in stock"
            options.append(discord.SelectOption(
                label=f"{p['name']} ({p['id']})",
                description=f"{p['price_ltc']} LTC • {stock_str}",
                value=p['id'],
            ))
        super().__init__(
            placeholder="Select a product to view details",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        pid = self.values[0]
        product = get_product(DB_FILE, pid)
        if not product:
            await interaction.response.send_message(
                "❌ That product is no longer available.", ephemeral=True
            )
            return

        stock_count, stock_emoji = get_stock_status(DB_FILE, pid)
        if product["stock"] < 0:
            stock_str = "Unlimited"
        else:
            stock_str = f"{stock_count} in stock"

        em = discord.Embed(
            title=product["name"],
            description=product["description"],
            color=COLORS["primary"],
        )
        em.add_field(name="Price", value=f"**{product['price_ltc']} LTC**", inline=True)
        if product.get("price_usd"):
            em.add_field(name="USD", value=f"**${product['price_usd']:.2f}**", inline=True)
        em.add_field(name="Stock", value=stock_str, inline=False)
        em.add_field(name="Delivery", value=product["delivery"], inline=False)
        em.set_footer(text=f"Product ID: {pid} • Click Buy Now to continue")
        await interaction.response.edit_message(embed=em, view=ProductDetailView(pid))


class DashboardView(discord.ui.View):
    @discord.ui.button(label="Browse Shop", style=discord.ButtonStyle.success, emoji="🛒", row=0)
    async def browse_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_mod = _get_bot_module()
        await bot_mod.send_shop(interaction)

    @discord.ui.button(label="Buy Product", style=discord.ButtonStyle.primary, emoji="💸", row=0)
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BuyProductModal())

    @discord.ui.button(label="Order Status", style=discord.ButtonStyle.primary, emoji="📦", row=0)
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderStatusModal())

    @discord.ui.button(label="My Orders", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def orders_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_mod = _get_bot_module()
        await bot_mod.send_my_orders(interaction)

    @discord.ui.button(label="LTC Price", style=discord.ButtonStyle.secondary, emoji="🌕", row=1)
    async def price_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_mod = _get_bot_module()
        await bot_mod.send_ltc_price(interaction)


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="New", style=discord.ButtonStyle.success, emoji="🆕", custom_id="admin_panel_new", row=0)
    async def add_product_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        view = start_product_builder(interaction)
        await interaction.response.send_message(
            embed=build_live_embed(view.data),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Stock", style=discord.ButtonStyle.primary, emoji="🔍", custom_id="admin_panel_stock", row=0)
    async def check_stock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return
        await interaction.response.send_modal(CheckStockModal())

    @discord.ui.button(label="Balance", style=discord.ButtonStyle.primary, emoji="⚖️", custom_id="admin_panel_balance", row=0)
    async def check_balance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return
        await interaction.response.send_modal(CheckBalanceModal())

    @discord.ui.button(label="Analytics", style=discord.ButtonStyle.secondary, emoji="📈", custom_id="admin_panel_analytics", row=0)
    async def analytics_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from shopbot.database import get_sales_analytics
            analytics = get_sales_analytics(DB_FILE, 7)

            em = discord.Embed(
                title="📊 Sales Analytics (Last 7 Days)",
                color=COLORS["info"]
            )

            totals = analytics['totals']
            em.add_field(
                name="💰 Revenue",
                value=f"**{totals.get('total_revenue', 0):.8f} LTC**",
                inline=True
            )
            em.add_field(
                name="📦 Orders",
                value=f"**{totals.get('total_orders', 0)}**",
                inline=True
            )
            em.add_field(
                name="👥 Customers",
                value=f"**{totals.get('total_customers', 0)}**",
                inline=True
            )

            await interaction.followup.send(embed=em, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to load analytics: {e}", ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="admin_panel_edit", row=1)
    async def edit_product_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not owner_or_admin_check_interaction(interaction):
                await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
                return
            await interaction.response.send_modal(EditProductModal())
        except Exception as e:
            logging.error(f"edit_product_button error: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Could not open edit modal. Try using the `/editproduct` command instead.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Could not open edit modal. Try using the `/editproduct` command instead.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="admin_panel_delete", row=1)
    async def delete_product_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not owner_or_admin_check_interaction(interaction):
                await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
                return
            await interaction.response.send_modal(DeleteProductModal())
        except Exception as e:
            logging.error(f"delete_product_button error: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Could not open delete modal. Try using the `/deleteproduct` command instead.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Could not open delete modal. Try using the `/deleteproduct` command instead.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Restock", style=discord.ButtonStyle.success, emoji="📦", custom_id="admin_panel_restock", row=1)
    async def restock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not admin_or_seller_check_interaction(interaction):
                await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
                return
            await interaction.response.send_modal(RestockProductModal())
        except Exception as e:
            logging.error(f"restock_button error: {e}", exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Could not open restock modal. Try using the `/restock` command instead.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Could not open restock modal. Try using the `/restock` command instead.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Orders", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="admin_panel_orders", row=1)
    async def all_orders_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        bot_mod = _get_bot_module()
        await bot_mod.send_all_orders(interaction)

    @discord.ui.button(label="Health", style=discord.ButtonStyle.secondary, emoji="⚡", custom_id="admin_panel_health", row=2)
    async def db_health_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from shopbot.database import get_database_health
            health = get_database_health(DB_FILE)

            em = discord.Embed(
                title="🏥 Database Health Report",
                color=COLORS["info"]
            )

            em.add_field(
                name="💾 Database Size",
                value=f"**{health.get('database_size_mb', 0)} MB**",
                inline=True
            )

            em.add_field(
                name="📦 Products",
                value=f"**{health.get('products_count', 0)}**",
                inline=True
            )

            em.add_field(
                name="📦 Stock Items",
                value=f"**{health.get('stock_items_count', 0)}**",
                inline=True
            )

            await interaction.followup.send(embed=em, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to check database health: {e}", ephemeral=True)

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="admin_panel_stats", row=2)
    async def quick_stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            bot_mod = _get_bot_module()
            products = bot_mod.all_products()
            orders = bot_mod.all_orders()

            total_products = len(products)
            total_orders = len(orders)
            pending_orders = len([o for o in orders if o['status'] == 'pending'])
            completed_orders = len([o for o in orders if o['status'] == 'delivered'])

            total_stock = sum(p.get('stock', 0) for p in products if p.get('stock', 0) >= 0)
            unlimited_products = len([p for p in products if p.get('stock', 0) < 0])

            em = discord.Embed(
                title="📈 Quick Stats",
                color=COLORS["primary"]
            )

            em.add_field(
                name="📦 Products",
                value=f"**{total_products}** total\n**{unlimited_products}** unlimited stock",
                inline=True
            )

            em.add_field(
                name="📋 Orders",
                value=f"**{total_orders}** total\n**{pending_orders}** pending\n**{completed_orders}** completed",
                inline=True
            )

            em.add_field(
                name="📊 Stock",
                value=f"**{total_stock}** items in stock",
                inline=True
            )

            await interaction.followup.send(embed=em, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to load stats: {e}", ephemeral=True)

    @discord.ui.button(label="Audit", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="admin_panel_audit", row=2)
    async def audit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not owner_or_admin_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(AuditProductModal())

    @discord.ui.button(label="Wallet", style=discord.ButtonStyle.success, emoji="💰", custom_id="admin_panel_wallet", row=2)
    async def set_wallet_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_or_seller_check_interaction(interaction):
            await interaction.response.send_message("🚫 Admin or Seller only.", ephemeral=True)
            return
        bot_mod = _get_bot_module()
        await bot_mod.send_wallet_panel(interaction)


# AdminPanelView is registered in bot.py after the bot object is created.
