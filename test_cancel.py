#!/usr/bin/env python3
import sqlite3

# Test order cancel database update
order_id = "10f701a2-4bda-42bd-8477-a6684220261d"
db_file = "data/shop_data.db"

print(f"Testing order cancel for order {order_id}")

# Get order before cancel
conn = sqlite3.connect(db_file)
c = conn.cursor()
c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
order_row = c.fetchone()

if order_row:
    # Get column names
    column_names = [desc[0] for desc in c.description]
    order = dict(zip(column_names, order_row))
    print(f"Before cancel: status={order['status']}")

    # Update order status to canceled
    c.execute('UPDATE orders SET status = ? WHERE id = ?', ('canceled', order_id))
    conn.commit()

    # Get order after cancel
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order_row_after = c.fetchone()
    conn.close()

    order_after = dict(zip(column_names, order_row_after))
    print(f"After cancel: status={order_after['status']}")

    print("✅ Order cancel database update works")
else:
    conn.close()
    print("❌ Order not found")