import aiohttp
import asyncio
import json

async def check_sweep_tx():
    txid = 'f1213b46baa89b4bf1ff856ce2aea58b9cd220f191e2dbf6146a493edc61d225'
    
    print(f"Fetching transaction details from BlockCypher...")
    print(f"TXID: {txid}\n")
    
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    
                    print("="*70)
                    print("SWEEP TRANSACTION ANALYSIS")
                    print("="*70)
                    
                    print(f"\n✓ Transaction exists on blockchain")
                    print(f"  Status: {data.get('receive_count', 0)} confirmations")
                    print(f"  Hash: {data.get('hash')}")
                    
                    print(f"\n📥 INPUTS (Funds coming from):")
                    for i, inp in enumerate(data.get('inputs', []), 1):
                        addrs = inp.get('addresses', [])
                        addr = addrs[0] if addrs else 'Unknown'
                        output_value = inp.get('output_value', 0)
                        print(f"  {i}. {addr}")
                        print(f"     Value: {output_value} satoshis ({output_value / 1e8} LTC)")
                    
                    print(f"\n📤 OUTPUTS (Funds going to):")
                    total_out = 0
                    for i, out in enumerate(data.get('outputs', []), 1):
                        addrs = out.get('addresses', [])
                        addr = addrs[0] if addrs else 'Unknown'
                        value = out.get('value', 0)
                        spent_by = out.get('spent_by', None)
                        total_out += value
                        
                        status = "✓ Spent" if spent_by else "⏳ Unspent"
                        print(f"  {i}. {addr}")
                        print(f"     Value: {value} satoshis ({value / 1e8} LTC)")
                        print(f"     Status: {status}")
                    
                    print(f"\n💰 SUMMARY:")
                    print(f"  Total Input: {data.get('total', 0)} satoshis ({data.get('total', 0) / 1e8} LTC)")
                    print(f"  Total Output: {total_out} satoshis ({total_out / 1e8} LTC)")
                    print(f"  Fee: {data.get('fees', 0)} satoshis ({data.get('fees', 0) / 1e8} LTC)")
                    
                    # Check if any output goes to receiving address
                    with open('config.json') as f:
                        config = json.load(f)
                    receiving_addr = config['crypto']['receiving_address']
                    print(f"\n🔍 CHECKING RECEIVING ADDRESS:")
                    print(f"  Expected: {receiving_addr}")
                    
                    found = False
                    for i, out in enumerate(data.get('outputs', []), 1):
                        addrs = out.get('addresses', [])
                        if addrs and addrs[0] == receiving_addr:
                            found = True
                            print(f"  ✓ Found at output {i}! Value: {out.get('value', 0) / 1e8} LTC")
                    
                    if not found:
                        print(f"  ✗ NOT FOUND in any output!")
                        print(f"\n⚠️  FUNDS ARE NOT GOING TO THE CONFIGURED RECEIVING ADDRESS!")
                        print(f"    The sweep transaction is routing funds elsewhere.")
                    
                else:
                    print(f"Error: {r.status}")
                    text = await r.text()
                    print(text[:300])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(check_sweep_tx())
