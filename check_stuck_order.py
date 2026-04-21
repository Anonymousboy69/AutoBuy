import sqlite3

conn = sqlite3.connect('data/shop_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("SELECT id, status, ltc_address, payment_txid, swept_at, sweep_txid, sweep_attempts, created_at FROM orders WHERE status='paid' LIMIT 5;")
for row in c.fetchall():
    print(f"ID: {row['id'][:8]}")
    print(f"Status: {row['status']}")
    print(f"LTC Address: {row['ltc_address']}")
    print(f"Payment TXID: {row['payment_txid']}")
    print(f"Sweep TXID: {row['sweep_txid']}")
    print(f"Sweep Attempts: {row['sweep_attempts']}")
    print(f"Swept at: {row['swept_at']}")
    print(f"Created: {row['created_at']}")
    print("---")
conn.close()
