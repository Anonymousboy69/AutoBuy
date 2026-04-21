import asyncio
import sqlite3
import aiohttp
from decimal import Decimal

# Convert litoshis to LTC
def litoshi_to_ltc(litoshi):
    return Decimal(str(litoshi)) / Decimal('1e8')

# Get BlockCypher token from config
def get_token():
    import json
    try:
        with open('config.json') as f:
            config = json.load(f)
        # For now, use a test token since we don't have the actual token loaded
        return "test"
    except:
        return "test"

async def get_address_transactions(address):
    """Fetch all incoming transactions for an address from BlockCypher"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}?txn=true"
            # Try without token first
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    txns = data.get('txrefs', [])
                    return txns
                else:
                    print(f"Error: {r.status}")
    except Exception as e:
        print(f"Error: {e}")
    return []

async def fix_stuck_order():
    # The stuck order
    order_id = "91c6a1e4-f5a2-4155-be0c-717106d3e5ae"
    address = "ltc1qfve9cg2xllnlx8puw8uh3u9r7dssstzk3g9lpk"
    expected_amount = Decimal("0.00089944")
    tolerance = Decimal('0.0001')  # Allow up to 0.0001 LTC variation (includes overpayments)
    
    print(f"Fixing stuck order {order_id[:8]}")
    print(f"Address: {address}")
    print(f"Expected amount: {expected_amount} LTC")
    
    # Get transactions
    print("\nFetching transactions...")
    transactions = await get_address_transactions(address)
    print(f"Found {len(transactions)} transactions")
    
    # Find the most recent transaction that matches the expected amount
    payment_txid = None
    payment_confirmations = 0
    
    # Sort by confirmations (ascending = most recent first)
    sorted_txs = sorted(transactions, key=lambda t: t.get('confirmations', float('inf')))
    
    for tx in sorted_txs:
        value_ltc = litoshi_to_ltc(tx.get('value', 0))
        print(f"\nTransaction: {tx.get('tx_hash')}")
        print(f"  Value: {value_ltc} LTC")
        print(f"  Confirmations: {tx.get('confirmations', 0)}")
        print(f"  Spent: {tx.get('spent')}")
        
        if tx.get('value', 0) > 0:  # Incoming transaction
            # Accept if:
            # 1. At or above expected amount
            # 2. Not more than 20% overpayment
            if (value_ltc >= expected_amount - tolerance and 
                value_ltc <= expected_amount * Decimal('1.2')):
                
                # Use the first (most recent) matching transaction
                if payment_txid is None:
                    payment_txid = tx.get('tx_hash')
                    payment_confirmations = tx.get('confirmations', 0)
                    print(f"  >>> SELECTED! (most recent matching transaction)")
                else:
                    print(f"  >>> Skipped (already have more recent one)")
    
    if not payment_txid:
        print("\nNo matching transaction found!")
        print("Transactions would need to be:")
        print(f"  Min: {expected_amount - tolerance} LTC")
        print(f"  Max: {expected_amount * Decimal('1.2')} LTC")
        return
    
    print(f"\n✅ Found payment transaction: {payment_txid}")
    print(f"   Confirmations: {payment_confirmations}")
    
    # Update the database
    print("\nUpdating database...")
    conn = sqlite3.connect('data/shop_data.db')
    c = conn.cursor()
    c.execute(
        "UPDATE orders SET payment_txid = ?, payment_confirmations = ? WHERE id = ?",
        (payment_txid, payment_confirmations, order_id)
    )
    conn.commit()
    conn.close()
    
    print("✅ Database updated!")
    print("\nNow trigger the bot to sweep this order:")
    print("1. Restart the bot, or")
    print("2. Wait for the next payment check poll")

asyncio.run(fix_stuck_order())
