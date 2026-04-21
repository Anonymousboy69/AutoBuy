#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from shopbot.database import all_orders

orders = all_orders('data/shop_data.db')
print(f"Found {len(orders)} orders")
for order in orders[:5]:
    print(f"Order {order['id']}: status={order['status']}, user_id={order['user_id']}")