import sqlite3
from datetime import datetime, timezone

# Order details
order_id = "407661ee"
user_id = "1385654097402658949"
product_id = "Product-db9e3d"
price_ltc = 0.00090810
ltc_address = "ltc1qfve9cg2x1lnlx8puw8uh3u9r7dssstzk3g9lpk"
address_path = "m/0/0"  # We don't know the exact path, use default
address_index = 0
created_at = datetime(2026, 4, 10, 17, 20, 2, tzinfo=timezone.utc).timestamp()

conn = sqlite3.connect('shop_data.db')
c = conn.cursor()

try:
    c.execute('''INSERT INTO orders (
                     id, user_id, product_id, price_ltc, ltc_address,
                     address_path, address_index, invoice_channel_id, invoice_message_id,
                     status, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (order_id, user_id, product_id, price_ltc, ltc_address,
               address_path, address_index,
               None,  # invoice_channel_id
               None,  # invoice_message_id
               'pending', created_at))
    
    conn.commit()
    print("✅ Order inserted successfully!")
    print(f"Order ID: {order_id}")
    print(f"Status: pending")
    print(f"Amount: {price_ltc} LTC")
    print(f"Address: {ltc_address}")
except Exception as e:
    print(f"❌ Error inserting order: {e}")
finally:
    conn.close()
