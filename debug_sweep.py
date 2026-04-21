#!/usr/bin/env python3
"""
Debug script to check sweep configuration and verify transaction
"""
import asyncio
import sqlite3
import sys
import os
from decimal import Decimal

# Add repo to path
sys.path.insert(0, os.path.dirname(__file__))

# Try to import
try:
    from shopbot.crypto import get_address_balance
    from utils import DB_FILE, RECEIVING_ADDRESS, WALLET_SEED, LTC_CONFIRMATIONS
    from utils import get_next_blockcypher_token
except Exception as e:
    print(f"Import error: {e}")
    # Fallback
    DB_FILE = 'data/shop_data.db'
    RECEIVING_ADDRESS = 'LfFRExQDy7LzvrDcAQpFsDyBATNSrdLh6W'
    WALLET_SEED = os.getenv('WALLET_SEED')
    LTC_CONFIRMATIONS = 1
    
    async def get_address_balance(addr, token):
        return None
    
    def get_next_blockcypher_token():
        return None

async def debug_sweep():
    print(f"=" * 60)
    print("SWEEP CONFIGURATION DEBUG")
    print(f"=" * 60)
    
    print(f"\n1. Configuration:")
    print(f"   Receiving Address: {RECEIVING_ADDRESS}")
    print(f"   LTC Confirmations: {LTC_CONFIRMATIONS}")
    print(f"   Wallet Seed Length: {len(WALLET_SEED) if WALLET_SEED else 'NOT SET'}")
    print(f"   DB File: {DB_FILE}")
    
    # Get the test order
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id LIKE "92802e44%" LIMIT 1')
    order = c.fetchone()
    conn.close()
    
    if not order:
        print("\n❌ Order not found!")
        return
    
    order = dict(order)
    print(f"\n2. Order Details:")
    print(f"   ID: {order['id'][:8]}...")
    print(f"   LTC Address: {order['ltc_address']}")
    print(f"   Address Path: {order['address_path']}")
    print(f"   Price LTC: {order['price_ltc']}")
    print(f"   Status: {order['status']}")
    print(f"   Sweep TXID: {order['sweep_txid']}")
    print(f"   Sweep Attempts: {order['sweep_attempts']}")
    
    # Check address balance
    print(f"\n3. Checking Payment Address Balance...")
    try:
        token = get_next_blockcypher_token()
        balance = await get_address_balance(order['ltc_address'], token)
        if balance:
            print(f"   Confirmed Balance: {balance.get('balance', 0)} litoshis")
            print(f"   Unconfirmed Balance: {balance.get('unconfirmed_balance', 0)} litoshis")
            print(f"   Total Received: {balance.get('total_received', 0)} litoshis")
        else:
            print(f"   ❌ Could not fetch balance")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    # Check if sweep TXID is valid
    if order['sweep_txid']:
        print(f"\n4. Sweep Transaction Details:")
        print(f"   TXID: {order['sweep_txid']}")
        print(f"   Note: To verify this transaction, check:")
        print(f"   https://ltc.blockchair.com/transaction/{order['sweep_txid']}")
        print(f"   Or in BlockCypher: https://www.blockcypher.com/ltc/tx/{order['sweep_txid']}")
    
    print(f"\n5. ISSUE DIAGNOSIS:")
    if order['sweep_txid']:
        print(f"   ✓ Sweep transaction WAS created and broadcast")
        print(f"   ? But LTC not received at: {RECEIVING_ADDRESS}")
        print(f"\n   Possible causes:")
        print(f"   1. Transaction still pending confirmation")
        print(f"   2. Sweep sent to wrong address (check tx output)")
        print(f"   3. Sweep transaction failed/was rejected")
        print(f"   4. Receiving address is incorrect in config")
    else:
        print(f"   ✗ No sweep transaction was created")

if __name__ == '__main__':
    asyncio.run(debug_sweep())
