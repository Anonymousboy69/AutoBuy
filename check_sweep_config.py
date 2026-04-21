import sqlite3
import json

# Read config
with open('config.json') as f:
    config = json.load(f)

print('SWEEP CONFIGURATION DEBUG')
print('=' * 60)
print(f'Receiving Address: {config["crypto"]["receiving_address"]}')
print(f'LTC Confirmations: {config["crypto"]["ltc_confirmations"]}')

# Get the test order
conn = sqlite3.connect('data/shop_data.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute('SELECT id, ltc_address, address_path, status, sweep_txid FROM orders WHERE id LIKE ? LIMIT 1', ['92802e44%'])
row = c.fetchone()
conn.close()

if row:
    order = dict(row)
    print(f'\nOrder Details:')
    print(f'  ID: {order["id"][:8]}...')
    print(f'  LTC Address: {order["ltc_address"]}')
    print(f'  Address Path: {order["address_path"]}')
    print(f'  Status: {order["status"]}')
    print(f'  Sweep TXID: {order["sweep_txid"]}')
    print(f'\nSweep Transaction Info:')
    if order['sweep_txid']:
        print(f'  ✓ Sweep transaction WAS broadcast')
        print(f'  TXID: {order["sweep_txid"]}')
        print(f'  Check at: https://www.blockcypher.com/ltc/tx/{order["sweep_txid"]}')
    else:
        print(f'  ✗ No sweep transaction was created')
else:
    print('Order not found')
