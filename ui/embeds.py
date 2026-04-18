# ─────────────────────────────────────────────
#  EMBED BUILDERS
# ─────────────────────────────────────────────
import json
import discord
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict
from urllib.parse import quote_plus

from shopbot.database import get_db, get_product as db_get_product, get_stock_items as db_get_stock_items, get_user_wallet as db_get_user_wallet
from shopbot.shop import get_stock_status
from shopbot.crypto import litoshi_to_ltc, format_ltc

from utils import (
    CONFIG, DB_FILE, COLORS, PAYMENT_TIMEOUT, LTC_CONFIRMATIONS, RESTOCKING_STATUS,
    mask_wallet_address, format_usd, ADMIN_ROLE_ID, SELLER_ROLE_ID,
    get_expiration_footer, get_expiration_timestamp,
)


def get_product(product_id: str) -> Optional[dict]:
    return db_get_product(DB_FILE, product_id)


def get_stock_items(product_id: str, status: str = None) -> List[dict]:
    return db_get_stock_items(DB_FILE, product_id, status)


def get_user_wallet(user_id: str) -> Optional[dict]:
    return db_get_user_wallet(DB_FILE, user_id)


def get_visible_stock_items(product_id: str, user: discord.User, roles: List[discord.Role], guild: discord.Guild | None = None, status: str | None = None) -> List[dict]:
    if guild and guild.owner_id == user.id:
        return get_stock_items(product_id, status)
    if any(r.id == ADMIN_ROLE_ID for r in roles):
        return get_stock_items(product_id, status)
    if any(r.id == SELLER_ROLE_ID for r in roles):
        seller_id = str(user.id)
        conn = get_db(DB_FILE)
        c = conn.cursor()
        if status:
            c.execute(
                'SELECT * FROM stock_items WHERE product_id = ? AND status = ? AND restocked_by = ? ORDER BY created_at ASC',
                (product_id, status, seller_id)
            )
        else:
            c.execute(
                'SELECT * FROM stock_items WHERE product_id = ? AND restocked_by = ? ORDER BY created_at ASC',
                (product_id, seller_id)
            )
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows
    return []


def default_embed_data() -> dict:
    return {
        "title":         "",
        "description":   "",
        "category":      None,
        "author_name":   "",
        "author_url":    "",
        "thumbnail_url": "",
        "image_url":     "",
        "footer_text":   "",
        "footer_url":    "",
        "price_ltc":     None,
        "price_usd":     None,
        "stock":         None,
        "delivery":      "",
        "color":         0x9B59B6,
        "fields":        [],
    }


def product_to_builder_data(product: dict) -> dict:
    data = default_embed_data()
    if product.get("embed_data"):
        try:
            loaded = json.loads(product["embed_data"]) or {}
            # Only update with valid data
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if key in data:
                        data[key] = value
        except Exception:
            pass
    
    # Ensure all string fields are strings
    for key in ["title", "description", "author_name", "author_url", "thumbnail_url", "image_url", "footer_text", "footer_url", "delivery"]:
        if data.get(key) is None or not isinstance(data[key], str):
            data[key] = ""
    
    # Ensure fields list is a list
    if not isinstance(data.get("fields"), list):
        data["fields"] = []
    
    # Validate color is an integer
    if not isinstance(data.get("color"), int):
        data["color"] = 0x9B59B6
    
    # Override with product values
    data["title"] = product.get("name") or data["title"]
    if product.get("description") is not None and isinstance(product["description"], str):
        data["description"] = product["description"]
    if product.get("delivery") is not None and isinstance(product["delivery"], str):
        data["delivery"] = product["delivery"]
    if product.get("category"):
        data["category"] = product["category"]
    data["price_ltc"] = product.get("price_ltc")
    data["price_usd"] = product.get("price_usd")
    data["stock"] = product.get("stock")
    
    return data


def build_live_embed(data: dict, pid: str = None, skip_description: bool = False) -> discord.Embed:
    desc = None if skip_description else (data.get("description") or "Add a product title and description using the buttons below.")
    em = discord.Embed(
        title       = data.get("title") or None,
        description = desc,
        color       = int(data.get("color", 0x9B59B6)),
    )
    
    # Set author only if it has non-empty content
    author_name = data.get("author_name", "").strip() if isinstance(data.get("author_name"), str) else ""
    if author_name:
        author_url = data.get("author_url", "").strip() if isinstance(data.get("author_url"), str) else ""
        if author_url and author_url.startswith("http"):
            em.set_author(name=author_name, url=author_url)
        else:
            em.set_author(name=author_name)
    
    thumbnail_url = data.get("thumbnail_url", "").strip() if isinstance(data.get("thumbnail_url"), str) else ""
    if thumbnail_url and thumbnail_url.startswith("http"):
        em.set_thumbnail(url=thumbnail_url)
    
    image_url = data.get("image_url", "").strip() if isinstance(data.get("image_url"), str) else ""
    if image_url and image_url.startswith("http"):
        em.set_image(url=image_url)

    if data.get("price_ltc") is not None:
        em.add_field(name="💰 Price", value=f"**{data['price_ltc']} LTC**", inline=True)
        if data.get("price_usd") is not None:
            em.add_field(name="💵 USD", value=f"**${data['price_usd']:.2f}**", inline=True)
    if data.get("stock") is not None:
        if pid is not None and data["stock"] >= 0:
            stock_count, _ = get_stock_status(DB_FILE, pid)
            stock_str = f"{stock_count} in stock"
        else:
            stock_str = "∞ Unlimited" if data["stock"] < 0 else f"{data['stock']} in stock"
        em.add_field(name="📦 Stock", value=stock_str, inline=True)
    
    delivery = data.get("delivery", "").strip() if isinstance(data.get("delivery"), str) else ""
    if delivery:
        em.add_field(name="📝 Delivery", value=delivery, inline=False)

    for f in data.get("fields", []):
        if isinstance(f, dict) and f.get("name") and f.get("value"):
            try:
                em.add_field(name=str(f["name"]), value=str(f["value"]), inline=bool(f.get("inline", False)))
            except Exception:
                continue

    custom_footer = data.get("footer_text", "").strip() if isinstance(data.get("footer_text"), str) else ""
    footer_icon = data.get("footer_url", "").strip() if isinstance(data.get("footer_url"), str) else ""
    
    if not footer_icon.startswith("http"):
        footer_icon = None

    if pid:
        footer_text = f"{custom_footer}  •  Product ID: {pid}" if custom_footer else f"Product ID: {pid}"
    else:
        if custom_footer:
            footer_text = f"{custom_footer}  •  Product ID: auto-assigned on save"
        else:
            footer_text = "Use buttons below to build your product card, then Save. Product ID is auto-assigned on save."

    if footer_icon:
        em.set_footer(text=footer_text, icon_url=footer_icon)
    else:
        em.set_footer(text=footer_text)

    return em


def build_no_stock_embed(product_id: str, seller_only: bool = False) -> discord.Embed:
    product = get_product(product_id)
    color = COLORS["warning"]

    if product:
        title = "🔎 Seller Stock Empty" if seller_only else "🔴 No Stock Available"
        description = (
            "You can only see your own pending stock items here.\n"
            "Add stock for this product to show it in the seller stock panel."
        ) if seller_only else f"{product['name']} currently has no pending stock items."
    else:
        title = "🔴 No Stock Available"
        description = "No stock information available."

    if product:
        description = (
            f"{product['name']} — `{product_id}`\n\n" + description
        )

    em = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    em.set_footer(
        text=(
            "Visible only: your own pending stock items." if seller_only
            else "Stock is currently unavailable."
        )
    )

    return em


def build_restock_embed(product_id: str, user: discord.User | None = None, roles: List[discord.Role] | None = None, guild: discord.Guild | None = None) -> discord.Embed:
    product = get_product(product_id)
    if not product:
        return discord.Embed(title="❌ Product not found", color=COLORS["error"])

    if user is not None and roles is not None:
        items = get_visible_stock_items(product_id, user, roles, guild, RESTOCKING_STATUS)
    else:
        items = get_stock_items(product_id, RESTOCKING_STATUS)

    stock_count, _ = get_stock_status(DB_FILE, product_id)
    stock_label = "Unlimited" if stock_count == float('inf') else f"{stock_count} items"

    em = discord.Embed(
        title       = f"📦 Restock — {product['name']}",
        color       = COLORS["primary"],
    )
    em.add_field(name="Staged Items", value=str(len(items)), inline=True)
    em.add_field(name="Product ID", value=f"`{product_id}`", inline=True)
    em.add_field(name="Available Stock", value=stock_label, inline=True)
    return em


def build_invoice_embed(order: dict, product: dict, balance_info: dict | None = None, processing: bool = False, transactions: list | None = None) -> discord.Embed:
    created_at = datetime.fromtimestamp(order['created_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    confirmed = litoshi_to_ltc(balance_info.get('balance', 0)) if balance_info else Decimal('0')
    unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0)) if balance_info else Decimal('0')
    expected = Decimal(str(order['price_ltc'])).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
    status = order['status'].capitalize()
    details = []

    if order['status'] == 'pending':
        if confirmed >= expected:
            status = 'Paid'
            details.append('Confirmed payment received.')
        elif unconfirmed >= expected:
            status = 'Awaiting Confirmation'
            details.append(f'Payment detected and waiting for {LTC_CONFIRMATIONS} confirmations.')
        elif confirmed > 0 or unconfirmed > 0:
            status = 'Partial Payment'
            details.append('Partial payment received.')
        else:
            details.append('Waiting for payment.')
    elif order['status'] == 'paid':
        if order.get('swept_at'):
            status = 'Paid • Swept'
            details.append('Funds swept to the receiving wallet.')
        else:
            status = 'Paid • Sweeping'
            details.append('Payment received, sweeping funds to the main wallet.')
    elif order['status'] == 'delivered':
        status = 'Delivered'
        details.append('Order delivered to the buyer.')
    elif order['status'] == 'failed':
        status = 'Failed'
        details.append('Delivery failed — admin review required.')
    elif order['status'] == 'expired':
        status = 'Expired'
        details.append('Order expired because payment was not received in time.')
    elif order['status'] == 'canceled':
        status = 'Canceled'
        details.append('This order was canceled by the buyer.')
    elif order['status'] == 'refunded':
        status = 'Refunded'
        if order.get('refund_txid'):
            details.append(f'Payment automatically refunded (txid: {order["refund_txid"][:16]}...).')
        else:
            details.append('Payment refund pending.')
    elif order['status'] == 'sweep_failed':
        status = 'Sweep Failed'
        details.append(f'Sweep failed after {order.get("sweep_attempts", 0)} attempts — admin review required.')

    if order.get('sweep_attempts', 0) > 0:
        details.append(f"Sweep attempts: {order['sweep_attempts']}")

    if not details:
        details.append('No updates yet.')

    title = f"📄 Invoice — Order {order['id'][:8]}"
    if order['status'] == 'canceled':
        title = f"❌ Order Canceled — {order['id'][:8]}"
    elif order['status'] == 'refunded':
        title = f"💰 Order Refunded — {order['id'][:8]}"

    show_payment_info = order['status'] not in {'canceled', 'expired', 'refunded'}

    em = discord.Embed(
        title=title,
        description='\n'.join(details),
        color=COLORS['info'] if order['status'] in {'pending', 'paid'} else COLORS['success'] if order['status'] in {'delivered', 'refunded'} else COLORS['warning'] if order['status'] == 'expired' else COLORS['error'],
    )

    em.add_field(name='Buyer', value=f"<@{order['user_id']}>" if order['user_id'] else 'Unknown', inline=True)
    em.add_field(name='Product', value=product['name'] if product else 'Unknown', inline=True)
    em.add_field(name='Status', value=status, inline=True)
    em.add_field(name="Blockchain", value="Litecoin", inline=True)

    em.add_field(name='Quantity', value=str(order.get('quantity', 1)), inline=True)
    em.add_field(name='Amount', value=f"{format_ltc(expected)} LTC", inline=True)
    if product and product.get('price_usd') is not None:
        if order.get('quantity', 1) > 1:
            total_usd = float(product['price_usd'] * order['quantity'])
            em.add_field(name='USD Total', value=f"${total_usd:.2f}", inline=True)
        else:
            em.add_field(name='USD Price', value=f"${product['price_usd']:.2f}", inline=True)
    if processing:
        em.add_field(name='Processing', value='Delivery is being processed. This may take a few seconds.', inline=False)
    if LTC_CONFIRMATIONS and LTC_CONFIRMATIONS > 1:
        em.add_field(name='Confirmations Required', value=str(LTC_CONFIRMATIONS), inline=True)

    if show_payment_info:
        em.add_field(name='Payment Address', value=f"```{order['ltc_address']}```", inline=False)

        # Only show blockchain explorer link if payment has been detected
        if order.get('ltc_address') and (confirmed > 0 or unconfirmed > 0):
            explorer_url = f"https://live.blockcypher.com/ltc/address/{order['ltc_address']}"
            em.add_field(name='🔗 Blockchain Explorer', value=f"[View Address]({explorer_url})", inline=False)
            if transactions:
                tx_links = []
                for tx in transactions[:5]:
                    tx_hash = tx.get('tx_hash') or tx.get('hash')
                    if tx_hash:
                        tx_links.append(f"[Tx {tx_hash[:8]}](https://live.blockcypher.com/ltc/tx/{tx_hash})")
                if tx_links:
                    if len(transactions) > 5:
                        tx_links.append(f"...and {len(transactions) - 5} more")
                    em.add_field(name='🧾 Recent Txns', value=' '.join(tx_links), inline=False)

    em.add_field(name='Confirmed', value=f"{format_ltc(confirmed)} LTC", inline=True)
    em.add_field(name='Unconfirmed', value=f"{format_ltc(unconfirmed)} LTC", inline=True)
    if confirmed < expected:
        remaining = expected - confirmed
        em.add_field(name='Remaining', value=f"{format_ltc(remaining)} LTC", inline=True)
    if confirmed > expected:
        em.add_field(name='Overpaid', value=f"{format_ltc(confirmed - expected)} LTC", inline=True)
    if not show_payment_info:
        em.add_field(name='Note', value='This order was canceled before payment. No blockchain payment is expected.', inline=False)

    em.add_field(name='Created', value=created_at, inline=False)

    if order.get('swept_at'):
        swept_at = datetime.fromtimestamp(order['swept_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        em.add_field(name='Swept At', value=swept_at, inline=False)
        
        # Add sweep transaction link if available
        if order.get('sweep_txid'):
            sweep_tx_url = f"https://live.blockcypher.com/ltc/tx/{order['sweep_txid']}"
            em.add_field(name='💸 Sweep Transaction', value=f"[View Sweep Tx]({sweep_tx_url})", inline=False)

    footer_text = 'Live invoice tracking — updates automatically as payment arrives.'
    if order['status'] == 'pending':
        footer_text = get_expiration_footer(order['created_at'], PAYMENT_TIMEOUT)
    elif order['status'] == 'expired':
        footer_text = 'Order expired — payment window closed.'

    if footer_text:
        em.set_footer(text=footer_text)
    return em


def build_wallet_embed(user_id: str, balance_info: dict | None = None, revenue_data: dict | None = None, payout_history: List[dict] | None = None, all_payouts: List[dict] | None = None, price_usd: float | None = None, recent_transactions: list | None = None) -> discord.Embed:
    wallet = get_user_wallet(user_id)
    address = wallet.get("ltc_address") if wallet else None
    status = "✅ Wallet Linked" if address else "❌ Not Linked"
    address_field = mask_wallet_address(address) if wallet else "No wallet linked."
    linked_by = wallet.get("linked_by_admin") if wallet else None
    notes = "Your linked LTC wallet is required for restocking and payouts."
    if not address:
        notes = "Link your wallet to enable restocking and secure payouts."

    em = discord.Embed(
        title="💰 Wallet Center",
        description="Manage your personal LTC wallet, view live balance, and track payouts.",
        color=COLORS["primary"],
    )
    em.add_field(name="Status", value=status, inline=True)

    if linked_by:
        em.add_field(name="Linked By", value=f"<@{linked_by}>", inline=True)

    em.add_field(name="Linked Address", value=address_field, inline=False)

    if balance_info and address:
        confirmed = litoshi_to_ltc(balance_info.get('balance', 0))
        unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0))
        em.add_field(
            name="Wallet Balance",
            value=(
                f"**{format_ltc(confirmed)} LTC** confirmed\n"
                f"**{format_ltc(unconfirmed)} LTC** pending"
            ),
            inline=False,
        )
        if price_usd is not None:
            confirmed_usd = confirmed * Decimal(str(price_usd))
            unconfirmed_usd = unconfirmed * Decimal(str(price_usd))
            em.add_field(
                name="Wallet Value",
                value=(
                    f"**${format_usd(confirmed_usd)}** confirmed\n"
                    f"**${format_usd(unconfirmed_usd)}** pending"
                ),
                inline=False,
            )
        em.add_field(
            name="Balance Source",
            value=f"Live balance for your linked LTC address: {mask_wallet_address(address)}",
            inline=False,
        )
    elif address and all_payouts is None and payout_history is None and revenue_data is None and recent_transactions is None:
        em.add_field(
            name="Wallet Balance",
            value=f"Connected to: {mask_wallet_address(address)}",
            inline=False,
        )
    elif address:
        em.add_field(
            name="Wallet Balance",
            value="Unable to fetch the linked address balance. Press Refresh to retry.",
            inline=False,
        )

    if recent_transactions:
        tx_lines = []
        for tx in recent_transactions[:3]:
            amount = litoshi_to_ltc(tx.get('value', 0))
            direction = "Received" if tx.get('tx_input_n', -1) == -1 else "Sent"
            status = "Confirmed" if tx.get('confirmations', 0) > 0 else "Pending"
            confirmed_at = tx.get('confirmed', "unknown")
            date = confirmed_at.split('T')[0] if isinstance(confirmed_at, str) and 'T' in confirmed_at else confirmed_at
            tx_lines.append(f"{date} • {direction} {format_ltc(amount)} LTC • {status}")
        if tx_lines:
            em.add_field(name="Recent Activity", value="\n".join(tx_lines), inline=False)

    total_paid = Decimal('0')
    if all_payouts:
        for payout in all_payouts:
            if payout.get('status') == 'completed':
                total_paid += Decimal(str(payout.get('amount_ltc', 0)))

    if revenue_data:
        min_payout = Decimal(str(CONFIG.get("payouts", {}).get("minimum_payout", 0.001)))
        total_revenue = Decimal(str(revenue_data['total_revenue']))
        pending_payout = total_revenue - total_paid
        if pending_payout < 0:
            pending_payout = Decimal('0')
        next_threshold = max(Decimal('0'), min_payout - pending_payout)

        em.add_field(name="Total Earned", value=f"**{format_ltc(total_revenue)} LTC**", inline=True)
        em.add_field(name="Total Paid", value=f"**{format_ltc(total_paid)} LTC**", inline=True)
        em.add_field(name="Pending Payout", value=f"**{format_ltc(pending_payout)} LTC**", inline=False)
        payout_note = "Eligible now for payout." if pending_payout >= min_payout else f"Needs **{format_ltc(next_threshold)} LTC** more to reach minimum payout."
        em.add_field(name="Payout Status", value=payout_note, inline=False)

    if payout_history:
        payout_lines = []
        for payout in payout_history[:3]:
            status = payout.get('status', 'unknown').capitalize()
            amount = format_ltc(Decimal(str(payout.get('amount_ltc', 0))))
            created = datetime.fromtimestamp(payout.get('created_at', 0), timezone.utc).strftime('%Y-%m-%d')
            payout_lines.append(f"{created} • {amount} LTC • {status}")
        if payout_lines:
            em.add_field(name="Recent Payouts", value="\n".join(payout_lines), inline=False)

    em.add_field(name="Notes", value=notes, inline=False)
    em.set_footer(text="Use the buttons below to update, remove, or refresh your wallet info.")
    return em


def build_seller_wallet_embed(user_id: str, balance_info: dict | None = None, revenue_data: dict | None = None, payout_history: List[dict] | None = None, all_payouts: List[dict] | None = None, price_usd: float | None = None, recent_transactions: list | None = None) -> discord.Embed:
    wallet = get_user_wallet(user_id)
    address = wallet.get("ltc_address") if wallet else None
    
    em = discord.Embed(
        title="💰 Seller Wallet",
        description="Manage your LTC wallet and earnings",
        color=COLORS["primary"],
    )
    
    # Connection status
    if address:
        em.add_field(name="Status", value="✅ Connected", inline=True)
        em.add_field(name="Address", value=f"`{address[:12]}...{address[-8:]}`", inline=True)
    else:
        em.add_field(name="Status", value="❌ Not Connected", inline=True)
        em.add_field(name="Address", value="No wallet", inline=True)
    
    # Balance section
    if balance_info and address:
        confirmed = litoshi_to_ltc(balance_info.get('balance', 0))
        unconfirmed = litoshi_to_ltc(balance_info.get('unconfirmed_balance', 0))
        total = confirmed + unconfirmed
        
        em.add_field(name="Balance", value=f"**{format_ltc(confirmed)}** LTC", inline=True)
        em.add_field(name="Pending", value=f"**{format_ltc(unconfirmed)}** LTC", inline=True)
        
        if price_usd:
            usd_val = total * Decimal(str(price_usd))
            em.add_field(name="USD Value", value=f"**${format_usd(usd_val)}**", inline=True)
    elif address:
        em.add_field(name="Balance", value="—", inline=True)
        em.add_field(name="Pending", value="—", inline=True)
        em.add_field(name="USD Value", value="—", inline=True)
    
    # Revenue section
    if revenue_data:
        total_revenue = Decimal(str(revenue_data['total_revenue']))
        total_paid = Decimal('0')
        if all_payouts:
            for payout in all_payouts:
                if payout.get('status') == 'completed':
                    total_paid += Decimal(str(payout.get('amount_ltc', 0)))
        pending = total_revenue - total_paid
        if pending < 0:
            pending = Decimal('0')
        
        em.add_field(name="Total Earned", value=f"**{format_ltc(total_revenue)} LTC**", inline=True)
        em.add_field(name="Paid Out", value=f"**{format_ltc(total_paid)} LTC**", inline=True)
        em.add_field(name="Pending", value=f"**{format_ltc(pending)} LTC**", inline=True)
    
    # Recent transactions
    if recent_transactions:
        tx_lines = []
        for tx in recent_transactions[:2]:
            amount = litoshi_to_ltc(tx.get('value', 0))
            confirmed = "✓" if tx.get('confirmations', 0) > 0 else "◯"
            tx_lines.append(f"{confirmed} {format_ltc(amount)} LTC")
        if tx_lines:
            em.add_field(name="Recent Activity", value="\n".join(tx_lines), inline=False)
    
    # Recent payouts
    if payout_history:
        payout_lines = []
        for payout in payout_history[:2]:
            amount = format_ltc(Decimal(str(payout.get('amount_ltc', 0))))
            status = "✓" if payout.get('status') == 'completed' else "⏳"
            payout_lines.append(f"{status} {amount} LTC")
        if payout_lines:
            em.add_field(name="Recent Payouts", value="\n".join(payout_lines), inline=False)
    
    em.set_footer(text="Click buttons to manage wallet, view earnings, or refresh")
    return em
