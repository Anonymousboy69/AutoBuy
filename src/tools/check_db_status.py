#!/usr/bin/env python3
from shopbot.database import get_db

def check_db():
    conn = get_db('shop.db')
    c = conn.cursor()

    # Check delivered orders
    c.execute('SELECT COUNT(*) FROM orders WHERE status = "delivered"')
    delivered_orders = c.fetchone()[0]
    print(f"Delivered orders: {delivered_orders}")

    # Check stock items with seller attribution
    c.execute('SELECT COUNT(*) FROM stock_items WHERE restocked_by IS NOT NULL')
    attributed_stock = c.fetchone()[0]
    print(f"Stock items with seller attribution: {attributed_stock}")

    # Check linked wallets
    c.execute('SELECT COUNT(*) FROM user_wallets WHERE is_active = 1')
    linked_wallets = c.fetchone()[0]
    print(f"Linked wallets: {linked_wallets}")

    conn.close()

if __name__ == "__main__":
    check_db()