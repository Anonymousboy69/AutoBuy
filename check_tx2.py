import asyncio
import aiohttp
import sys

async def get_address_transactions(address, token):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json',
    }
    async with aiohttp.ClientSession(headers=headers) as s:
        url = f'https://api.blockcypher.com/v1/ltc/main/addrs/{address}?txn=true&token={token}'
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                return data.get('txrefs', [])
            else:
                print(f"Error: {r.status}")
    return []

async def main():
    # Get token from config
    import json
    with open('config.json') as f:
        config = json.load(f)
    
    # Get a token - for now just use a placeholder
    token = "test_token"
    
    address = 'ltc1qfve9cg2xllnlx8puw8uh3u9r7dssstzk3g9lpk'
    print(f'Checking address: {address}')
    
    txs = await get_address_transactions(address, token)
    print(f'Found {len(txs)} transactions:')
    for tx in txs[:5]:
        print(f"  TXID: {tx.get('tx_hash')}")
        print(f"  Value: {tx.get('value')} satoshis")
        print(f"  Confirmations: {tx.get('confirmations')}")
        print()

asyncio.run(main())
