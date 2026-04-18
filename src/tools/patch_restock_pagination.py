from pathlib import Path

path = Path('c:/Users/adity/Downloads/files/bot.py')
text = path.read_text(encoding='utf-8')
lines = text.splitlines(True)
start = next(i for i, l in enumerate(lines) if l.strip() == 'preview_items = []')
end = next(i for i, l in enumerate(lines[start:], start) if 'em.set_footer(text=f"Restocked by' in l)
new_block = [
    '        page_view = RestockPageView(self.product_id, added_items, interaction.user.name) if added_items else None\n',
    '        if page_view:\n',
    '            em = page_view.get_embed()\n',
    '            em.add_field(name="Items Added", value=str(added), inline=True)\n',
    '            em.add_field(name="Duplicates Skipped", value=str(duplicates), inline=True)\n',
    '            em.add_field(name="New Stock Count", value=str(new_stock), inline=True)\n',
    '        else:\n',
    '            em = discord.Embed(\n',
    '                title="📦 Bulk Stock Added",\n',
    '                description=f"Successfully added stock to product `{self.product_id}`",\n',
    '                color=COLORS["success"],\n',
    '            )\n',
    '            em.add_field(name="Items Added", value=str(added), inline=True)\n',
    '            em.add_field(name="Duplicates Skipped", value=str(duplicates), inline=True)\n',
    '            em.add_field(name="New Stock Count", value=str(new_stock), inline=True)\n',
    '            em.set_footer(text=f"Restocked by {interaction.user.name}")\n',
]
lines[start:end] = new_block
path.write_text(''.join(lines), encoding='utf-8')
print('patched')
