import sqlite3

conn = sqlite3.connect('shop_data.db')
c = conn.cursor()
c.execute('SELECT id, status, ltc_address, price_ltc, created_at FROM orders')
rows = c.fetchall()
print(rows)
conn.close()
