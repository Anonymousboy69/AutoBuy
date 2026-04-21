import sqlite3

conn = sqlite3.connect('data/shop_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute('SELECT * FROM orders WHERE id LIKE ?', ('91c6a1e4%',))
row = c.fetchone()
if row:
    for key in row.keys():
        print(f'{key}: {row[key]}')
else:
    print('Order not found')
conn.close()
