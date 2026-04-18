import sqlite3

conn = sqlite3.connect('shop_data.db')
c = conn.cursor()

# Check orders
c.execute('SELECT id, status, ltc_address, price_ltc FROM orders ORDER BY created_at DESC LIMIT 5')
rows = c.fetchall()

print('=== Orders in Database ===')
if rows:
    for row in rows:
        print(f'ID: {row[0][:8]}... | Status: {row[1]} | Address: {row[2]} | Price: {row[3]} LTC')
else:
    print('NO ORDERS FOUND')

conn.close()
