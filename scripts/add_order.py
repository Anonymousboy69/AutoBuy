#!/usr/bin/env python3
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('shop_data.db')
c = conn.cursor()
ts = datetime(2026, 4, 10, 17, 20, 2, tzinfo=timezone.utc).timestamp()

c.execute('''INSERT INTO orders 
    (id, user_id, product_id, price_ltc, ltc_address, address_path, address_index, status, created_at) 
VALUES 
    (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
('407661ee', '1385654097402658949', 'Product-db9e3d', 0.00090810, 'ltc1qfve9cg2x1lnlx8puw8uh3u9r7dssstzk3g9lpk', 'm/0/0', 0, 'pending', ts))

conn.commit()
print("Order inserted successfully!")
conn.close()
