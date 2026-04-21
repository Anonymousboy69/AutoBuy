import asyncio
import logging
from src.services.payment_engine import get_address_transactions
from shopbot.crypto import litoshi_to_ltc
from utils import get_next_blockcypher_token
from decimal import Decimal

logging.basicConfig(level=logging.INFO)

async def main():
    address = "ltc1qfve9cg2xllnlx8puw8uh3u9r7dssstzk3g9lpk"
    expected_amount = Decimal("0.00089944")  # From the invoice shown in the Discord message
    
    print(f"Checking address: {address}")
    print(f"Expected amount: {expected_amount} LTC")
    
    transactions = await get_address_transactions(address)
    print(f"Found {len(transactions)} transactions")
    
    for tx in transactions:
        print(f"\n---")
        print(f"TXID: {tx.get('tx_hash')}")
        print(f"Value: {litoshi_to_ltc(tx.get('value', 0))} LTC")
        print(f"Confirmations: {tx.get('confirmations', 0)}")
        print(f"Spent: {tx.get('spent')}")

asyncio.run(main())
