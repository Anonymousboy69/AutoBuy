class RestockView(discord.ui.View):
    def __init__(self, product_id: str, user: discord.User | None = None, roles: List[discord.Role] | None = None, guild: discord.Guild | None = None):
        super().__init__(timeout=300)
        self.product_id = product_id
        self.user = user
        self.roles = roles
        self.guild = guild

    @discord.ui.button(label="Add Single", style=discord.ButtonStyle.primary, custom_id="restock_add_single")
    async def add_single(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RestockModal(self.product_id, interaction.message.id, interaction.channel.id))

    @discord.ui.button(label="Add Multiple", style=discord.ButtonStyle.primary, custom_id="restock_add_multiple")
    async def add_multiple(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BulkRestockModal(self.product_id, interaction.message.id, interaction.channel.id))

    @discord.ui.button(label="View Staging", style=discord.ButtonStyle.secondary, custom_id="restock_view_staging")
    async def view_count(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = get_visible_stock_items(self.product_id, interaction.user, interaction.user.roles, interaction.guild, RESTOCKING_STATUS)
        product = get_product(self.product_id)

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
        if admin_check_interaction(interaction):
            c.execute("UPDATE stock_items SET status = ? WHERE product_id = ? AND status = ?", ("pending", self.product_id, RESTOCKING_STATUS))
        else:
            seller_id = str(interaction.user.id)
            c.execute("UPDATE stock_items SET status = ? WHERE product_id = ? AND status = ? AND restocked_by = ?", ("pending", self.product_id, RESTOCKING_STATUS, seller_id))
        moved_items = c.rowcount
        if moved_items > 0:
            c.execute("""UPDATE products SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending') WHERE stock >= 0 AND id = ?""", (self.product_id, self.product_id))
        conn.commit()
        conn.close()

        await update_stock_message(DB_FILE, self.product_id, interaction.client)

        total_stock, _ = get_stock_status(DB_FILE, self.product_id)
        total_stock_label = "Unlimited" if total_stock == float('inf') else f"{total_stock} items"

        done_embed = discord.Embed(title="✅ Restock Complete", description=f"Moved {moved_items} item{'s' if moved_items != 1 else ''} into stock.", color=COLORS["success"])
        done_embed.add_field(name="Product ID", value=f"`{self.product_id}`", inline=False)
        done_embed.add_field(name="Items Restocked", value=str(moved_items), inline=True)
        done_embed.add_field(name="Total Stock", value=total_stock_label, inline=True)
        done_embed.set_footer(text="Restocking finished.")

        try:
            await interaction.response.edit_message(embed=done_embed, view=None)
        except Exception:
            try:
                await interaction.message.edit(embed=done_embed, view=None)
            except Exception:
                await interaction.response.send_message(embed=done_embed, ephemeral=True)


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

        em.add_field(name="Product ID", value=f"`{self.product.get('id', self.product_id)}`", inline=True)
        em.add_field(name="Item ID", value=f"`{current_item['id']}`", inline=True)
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

        em.add_field(name="Item ID", value=f"`{current_item['id']}`", inline=True)
        em.add_field(name="Product ID", value=f"`{self.product_id}`", inline=True)
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
        await interaction.response.send_modal(EditItemModal(self.product_id, self.item_id, self.item_content, interaction.message, self.page))

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
        await interaction.response.send_modal(EditItemModal(self.product_id, self.item_id, self.item_content, interaction.message))

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

        product = get_product(self.product_id)
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