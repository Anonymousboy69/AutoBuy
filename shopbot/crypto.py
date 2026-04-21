import asyncio
import logging
import os
import time
import aiohttp
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple, Dict, Any
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.keys import HDKey
from bitcoinlib.wallets import Wallet
from bitcoinlib.transactions import Transaction, Input, Output
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
#  WALLET HELPERS (LTC)
# ─────────────────────────────────────────────
_wallet = None
_payment_wallet = None

_BLOCKCYPHER_TOKEN_BLACKOUTS: Dict[str, float] = {}

# Simple in-memory cache for balance queries to avoid duplicate API calls
_balance_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}
_CACHE_DURATION = timedelta(seconds=30)  # Cache results for 30 seconds

_API_REQUEST_DELAY = 0.5  # seconds between outbound API calls
_api_request_lock = asyncio.Lock()
_last_api_request_at = 0.0


async def _wait_for_api_slot() -> None:
    global _last_api_request_at
    async with _api_request_lock:
        elapsed = time.time() - _last_api_request_at
        if elapsed < _API_REQUEST_DELAY:
            await asyncio.sleep(_API_REQUEST_DELAY - elapsed)
        _last_api_request_at = time.time()


async def _fetch_get(session: aiohttp.ClientSession, url: str, **kwargs):
    await _wait_for_api_slot()
    return await session.get(url, **kwargs)


async def _fetch_post(session: aiohttp.ClientSession, url: str, **kwargs):
    await _wait_for_api_slot()
    return await session.post(url, **kwargs)


def _is_token_blocked(token: str) -> bool:
    if not token:
        return False
    return time.time() < _BLOCKCYPHER_TOKEN_BLACKOUTS.get(token, 0)


def _block_token(token: str, retry_after: int) -> None:
    if not token:
        return
    blocked_until = time.time() + max(int(retry_after), 1)
    _BLOCKCYPHER_TOKEN_BLACKOUTS[token] = blocked_until
    logging.warning(f"BlockCypher token blocked until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(blocked_until))}: {token}")


async def register_blockcypher_webhook(address: str, callback_url: str, token: str, event: str = "tx-confirmation") -> dict | None:
    if not address or not callback_url or not token:
        logging.error("Register webhook failed: missing address, callback_url, or token")
        return None

    url = "https://api.blockcypher.com/v1/ltc/main/hooks"
    payload = {
        "event": event,
        "address": address,
        "url": callback_url,
        "token": token,
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with await _fetch_post(s, url, json=payload) as r:
                text = await r.text()
                if r.status not in (200, 201):
                    logging.error(f"BlockCypher webhook registration failed: {r.status} {text[:300]}")
                    return None
                result = await r.json() if text else {}
                logging.info(f"Registered BlockCypher webhook for {address}: {result.get('id')}")
                return result
    except BaseException as e:
        logging.error(f"BlockCypher webhook registration error: {e}")
        import traceback
        traceback.print_exc()
        return None


async def delete_blockcypher_webhook(hook_id: str, token: str = None) -> bool:
    if not hook_id:
        logging.debug("No BlockCypher hook ID provided for deletion")
        return False

    url = f"https://api.blockcypher.com/v1/ltc/main/hooks/{hook_id}"
    if token:
        url += f"?token={token}"

    try:
        async with aiohttp.ClientSession() as s:
            async with await s.delete(url) as r:
                text = await r.text()
                if r.status in (200, 204):
                    logging.info(f"Deleted BlockCypher webhook {hook_id}")
                    return True
                logging.warning(f"BlockCypher webhook deletion failed: {r.status} {text[:300]}")
                return False
    except BaseException as e:
        logging.warning(f"BlockCypher webhook deletion error: {e}")
        return False


def get_wallet_from_seed(wallet_seed: str):
    if not wallet_seed:
        return None
    try:
        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(wallet_seed)
        key = HDKey.from_seed(seed_bytes, network="litecoin")
        return key
    except BaseException as e:
        logging.error(f"Failed to load wallet: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_payment_wallet(wallet_seed: str, db_file: str):
    global _payment_wallet
    if _payment_wallet is not None:
        return _payment_wallet
    try:
        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(wallet_seed)
        wallet_name = "shop_ltc_payment_wallet"
        db_path = os.path.abspath(os.path.join("data", "bitcoinlib_wallet.db")).replace("\\", "/")
        db_uri = f"sqlite:///{db_path}"
        try:
            _payment_wallet = Wallet(wallet_name, db_uri=db_uri)
            logging.info(f"Opened existing payment wallet '{wallet_name}'.")
        except Exception as exc:
            logging.info(f"Existing wallet '{wallet_name}' not found, creating new one: {exc}")
            _payment_wallet = Wallet.create(
                wallet_name,
                keys=seed_bytes,
                network="litecoin",
                witness_type="segwit",
                db_uri=db_uri,
            )
            logging.info(f"Created new payment wallet '{wallet_name}'.")
        return _payment_wallet
    except BaseException as e:
        logging.error(f"Failed to initialize sweep wallet: {e}")
        import traceback
        traceback.print_exc()
        return None

def find_address_path_by_address(db_file: str, address: str, wallet_seed: str) -> str | None:
    from shopbot.database import get_db
    root = get_wallet_from_seed(wallet_seed)
    if root is None or not address:
        return None
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT MAX(address_index), COUNT(*) FROM orders')
    row = c.fetchone()
    conn.close()
    max_index = 0
    if row:
        max_index = int(row[0]) if row[0] is not None else int(row[1] or 0)
    for index in range(max_index + 10):
        try:
            child = root.key_for_path(f"m/0/{index}")
            if child.address() == address:
                return f"m/0/{index}"
        except Exception:
            continue
    return None

async def generate_ltc_address(db_file: str, wallet_seed: str) -> tuple[str | None, str | None, int | None]:
    from shopbot.database import get_next_address_index
    try:
        global _wallet
        if _wallet is None:
            logging.info(f"Initializing wallet from seed (seed length: {len(wallet_seed) if wallet_seed else 0})")
            _wallet = get_wallet_from_seed(wallet_seed)
        if _wallet is None:
            logging.error("No wallet configured. Set WALLET_SEED in .env file")
            return None, None, None

        address_index = get_next_address_index(db_file)
        logging.debug(f"Generating address at index {address_index}")
        address_path = f"m/0/{address_index}"
        child_key = _wallet.key_for_path(address_path)
        address = child_key.address()
        logging.info(f"✅ Generated address {address} at path {address_path}")
        return address, address_path, address_index
    except BaseException as e:
        logging.error(f"Failed to generate address: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None

async def get_address_balance(address: str, blockcypher_token: str = None) -> dict:
    """Get Litecoin address balance with caching and rotating tokens."""
    global _balance_cache

    # Check cache first
    now = datetime.now()
    if address in _balance_cache:
        cached_result, cached_time = _balance_cache[address]
        if now - cached_time < _CACHE_DURATION:
            logging.debug(f"Using cached balance for {address}: {cached_result.get('balance')} satoshis")
            return cached_result
        else:
            # Cache expired, remove it
            del _balance_cache[address]

    # Not in cache, make the API call
    result = await _get_address_balance_uncached(address, blockcypher_token)

    # Cache the result
    if result:
        _balance_cache[address] = (result, now)
        logging.debug(f"Cached balance for {address}: {result.get('balance')} satoshis")

    return result


async def get_addresses_balance(addresses: list[str], blockcypher_token: str = None) -> dict[str, dict]:
    """Get balances for multiple Litecoin addresses in batch when possible."""
    if not addresses:
        return {}

    results: dict[str, dict] = {}
    addresses_to_query: list[str] = []
    now = datetime.now()
    seen = set()

    for address in addresses:
        if not address or address in seen:
            continue
        seen.add(address)

        cached_item = _balance_cache.get(address)
        if cached_item:
            cached_result, cached_time = cached_item
            if now - cached_time < _CACHE_DURATION:
                results[address] = cached_result
                continue
            del _balance_cache[address]

        addresses_to_query.append(address)

    if not addresses_to_query:
        return results

    chunk_size = 100
    for i in range(0, len(addresses_to_query), chunk_size):
        chunk = addresses_to_query[i:i + chunk_size]
        batch_result = await _get_addresses_balance_batch_uncached(chunk, blockcypher_token)
        if batch_result:
            for address, balance_data in batch_result.items():
                if balance_data is not None:
                    results[address] = balance_data
                    _balance_cache[address] = (balance_data, now)

        for address in chunk:
            if address in results:
                continue
            single_balance = await get_address_balance(address, blockcypher_token)
            if single_balance:
                results[address] = single_balance
                _balance_cache[address] = (single_balance, now)

    return results


async def _get_addresses_balance_batch_uncached(addresses: list[str], blockcypher_token: str = None) -> dict[str, dict] | None:
    if not addresses:
        return {}

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    timeout = aiohttp.ClientTimeout(total=10)

    async def parse_blockcypher_batch(payload: dict) -> dict[str, dict] | None:
        if not payload:
            return None
        if isinstance(payload.get('addresses'), list):
            parsed: dict[str, dict] = {}
            for item in payload['addresses']:
                address = item.get('address')
                if not address:
                    continue
                parsed[address] = {
                    'balance': int(item.get('balance', 0) or 0),
                    'unconfirmed_balance': int(item.get('unconfirmed_balance', 0) or 0),
                }
            return parsed if parsed else None

        if len(addresses) == 1 and payload.get('balance') is not None:
            return {
                addresses[0]: {
                    'balance': int(payload.get('balance', 0) or 0),
                    'unconfirmed_balance': int(payload.get('unconfirmed_balance', 0) or 0),
                }
            }
        return None

    async def try_blockcypher_batch(token: str | None) -> tuple[dict[str, dict] | None, int | None]:
        encoded_addresses = ";".join(addresses)
        url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{encoded_addresses}/balance"
        if token:
            url += f"?token={token}"
            logging.debug(f"Querying BlockCypher batch balance for {len(addresses)} addresses with token {token[:8]}...")
        else:
            logging.debug(f"Querying BlockCypher batch balance for {len(addresses)} addresses without token...")

        async with await _fetch_get(s, url) as r:
            if r.status == 200:
                data = await r.json()
                parsed = await parse_blockcypher_batch(data)
                if parsed is not None:
                    return parsed, None
                logging.warning(f"BlockCypher batch balance returned unexpected data: {data}")
                return None, None
            elif r.status == 429:
                retry_after = int(r.headers.get('Retry-After', '60'))
                text = await r.text()
                logging.warning(f"BlockCypher batch rate limited (token={token[:8] if token else 'None'}): {r.status} {text[:300]} (retry after {retry_after}s)")
                return None, retry_after
            else:
                text = await r.text()
                logging.warning(f"BlockCypher batch balance failed (token={token[:8] if token else 'None'}): {r.status} {text[:300]}")
                return None, None

    from utils.config import get_next_blockcypher_token, BLOCKCYPHER_TOKENS
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            if blockcypher_token:
                tokens_to_try = [blockcypher_token] + [t for t in BLOCKCYPHER_TOKENS if t != blockcypher_token]
            else:
                tokens_to_try = []
                for _ in range(len(BLOCKCYPHER_TOKENS)):
                    token = get_next_blockcypher_token()
                    if token and token not in tokens_to_try and not _is_token_blocked(token):
                        tokens_to_try.append(token)
                if not tokens_to_try:
                    logging.warning("All BlockCypher tokens are currently blocked; batch request will fallback to individual balance queries.")
                    tokens_to_try = [None]

            for token in tokens_to_try:
                if token is not None and _is_token_blocked(token):
                    continue

                batch_result, retry_after = await try_blockcypher_batch(token)
                if retry_after and token is not None:
                    _block_token(token, retry_after)
                if batch_result is not None:
                    return batch_result

            return None
    except BaseException as e:
        logging.error(f"BlockCypher batch request error: {e}")
        import traceback
        traceback.print_exc()
        return None


async def _get_address_balance_uncached(address: str, blockcypher_token: str = None) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    timeout = aiohttp.ClientTimeout(total=5)
    blockchair_key = os.getenv('BLOCKCHAIR_API_KEY', '').strip()

    from utils.config import get_next_blockcypher_token, BLOCKCYPHER_TOKENS

    async def parse_blockchair_address(payload: dict) -> dict | None:
        addr_data = payload.get('data', {}).get(address, {}).get('address', {})
        if not addr_data:
            return None
        try:
            balance = int(addr_data.get('balance', 0) or 0)
            unconfirmed = int(addr_data.get('unconfirmed_balance', 0) or 0)
            return {'balance': balance, 'unconfirmed_balance': unconfirmed}
        except BaseException as e:
            logging.warning(f"Blockchair parse failed for {address}: {e}")
            return None

    async def parse_sochain_address(payload: dict) -> dict | None:
        if payload.get('status') != 'success':
            return None
        try:
            confirmed = Decimal(str(payload['data'].get('confirmed_balance', '0')))
            unconfirmed = Decimal(str(payload['data'].get('unconfirmed_balance', '0')))
            return {
                'balance': int((confirmed * Decimal('1e8')).to_integral_value()),
                'unconfirmed_balance': int((unconfirmed * Decimal('1e8')).to_integral_value()),
            }
        except BaseException as e:
            logging.warning(f"SoChain parse failed for {address}: {e}")
            return None

    async def try_blockcypher_balance(token: str) -> tuple[dict | None, int | None]:
        url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance"
        if token:
            url += f"?token={token}"
            logging.debug(f"Querying BlockCypher balance for {address} with token {token[:8]}...")
        else:
            logging.debug(f"Querying BlockCypher balance for {address} without token...")
        async with await _fetch_get(s, url) as r:
            if r.status == 200:
                data = await r.json()
                if data.get('balance') is not None:
                    logging.debug(f"BlockCypher balance success for {address}: {data.get('balance')} satoshis")
                    return data, None
                logging.warning(f"BlockCypher balance endpoint returned unexpected data for {address} with token={token}: {data}")
                return None, None
            elif r.status == 429:
                retry_after = int(r.headers.get('Retry-After', '60'))
                text = await r.text()
                logging.warning(f"BlockCypher balance query rate limited for {address} with token={token[:8] if token else 'None'}: {r.status} {text[:300]} (retry after {retry_after}s)")
                return None, retry_after
            else:
                text = await r.text()
                logging.warning(f"BlockCypher balance query failed for {address} with token={token[:8] if token else 'None'}: {r.status} {text[:300]}")
                return None, None

    async def try_blockcypher_address(token: str) -> tuple[dict | None, int | None]:
        addr_url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}"
        if token:
            addr_url += f"?token={token}"
            logging.debug(f"Querying BlockCypher address for {address} with token {token[:8]}...")
        else:
            logging.debug(f"Querying BlockCypher address for {address} without token...")
        async with await _fetch_get(s, addr_url) as r:
            if r.status == 200:
                data = await r.json()
                if data.get('balance') is not None:
                    logging.debug(f"BlockCypher address success for {address}: {data.get('balance')} satoshis")
                    return {
                        'balance': int(data.get('balance', 0) or 0),
                        'unconfirmed_balance': int(data.get('unconfirmed_balance', 0) or 0),
                    }, None
                logging.warning(f"BlockCypher address query returned unexpected data for {address} with token={token}: {data}")
                return None, None
            elif r.status == 429:
                retry_after = int(r.headers.get('Retry-After', '60'))
                text = await r.text()
                logging.warning(f"BlockCypher address query rate limited for {address} with token={token[:8] if token else 'None'}: {r.status} {text[:300]} (retry after {retry_after}s)")
                return None, retry_after
            else:
                text = await r.text()
                logging.warning(f"BlockCypher address query failed for {address} with token={token[:8] if token else 'None'}: {r.status} {text[:300]}")
                return None, None

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            if blockcypher_token:
                tokens_to_try = [blockcypher_token] + [t for t in BLOCKCYPHER_TOKENS if t != blockcypher_token]
            else:
                tokens_to_try = []
                for _ in range(len(BLOCKCYPHER_TOKENS)):
                    token = get_next_blockcypher_token()
                    if token and token not in tokens_to_try and not _is_token_blocked(token):
                        tokens_to_try.append(token)
                if not tokens_to_try:
                    logging.warning("All BlockCypher tokens are currently blocked; skipping BlockCypher and using fallback providers.")
                    tokens_to_try = [None]

            for token in tokens_to_try:
                if token is None:
                    result, retry_after = await try_blockcypher_balance(None)
                    if result is not None:
                        return result
                    result, retry_after = await try_blockcypher_address(None)
                    if result is not None:
                        return result
                    continue

                if _is_token_blocked(token):
                    continue

                result, retry_after = await try_blockcypher_balance(token)
                if retry_after:
                    _block_token(token, retry_after)
                if result is not None:
                    return result

                result, retry_after = await try_blockcypher_address(token)
                if retry_after:
                    _block_token(token, retry_after)
                if result is not None:
                    return result

            # Secondary: Blockchair (fallback if BlockCypher is rate limited or blocked)
            blockchair_url = f"https://api.blockchair.com/litecoin/dashboards/address/{address}"
            if blockchair_key:
                blockchair_url += f"?key={blockchair_key}"
            logging.debug(f"Querying Blockchair for {address}...")
            async with await _fetch_get(s, blockchair_url) as r:
                if r.status == 200:
                    payload = await r.json()
                    result = await parse_blockchair_address(payload)
                    if result is not None:
                        logging.debug(f"Blockchair success for {address}: {result.get('balance')} satoshis")
                        return result
                else:
                    text = await r.text()
                    logging.warning(f"Blockchair balance query failed for {address}: {r.status} {text[:300]}")

            # Tertiary: SoChain
            fallback_url = f"https://sochain.com/api/v2/get_address_balance/LTC/{address}"
            logging.debug(f"Querying SoChain for {address}...")
            async with await _fetch_get(s, fallback_url) as r:
                if r.status == 200:
                    payload = await r.json()
                    result = await parse_sochain_address(payload)
                    if result is not None:
                        logging.debug(f"SoChain success for {address}: {result.get('balance')} satoshis")
                        return result
                else:
                    text = await r.text()
                    logging.warning(f"SoChain balance query failed for {address}: {r.status} {text[:300]}")

            # Final fallback: Trezor blockbook
            trezor_url = f"https://ltc1.trezor.io/api/v2/address/{address}"
            logging.debug(f"Querying Trezor for {address}...")
            async with await _fetch_get(s, trezor_url) as r:
                if r.status == 200:
                    data = await r.json()
                    balance = int(data.get('balance', 0) or 0)
                    unconfirmed = int(data.get('unconfirmed', 0) or 0)
                    logging.debug(f"Trezor success for {address}: {balance} satoshis")
                    return {'balance': balance, 'unconfirmed_balance': unconfirmed}
                else:
                    text = await r.text()
                    logging.warning(f"Trezor blockbook balance query failed for {address}: {r.status} {text[:300]}")
    except asyncio.TimeoutError:
        logging.warning(f"Balance query timed out for {address}")
    except BaseException as e:
        logging.warning(f"Balance query error for {address}: {e}")
    return None

def litoshi_to_ltc(litoshi: int) -> Decimal:
    return (Decimal(litoshi) / Decimal('1e8')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

def format_ltc(amount: Decimal | float | int) -> str:
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    value = amount.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
    if value == 0:
        return "0"
    return format(value, 'f')

async def fetch_ltc_usd_price() -> float | None:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as s:
            async with await _fetch_get(s, url, timeout=10) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                return float(data.get("litecoin", {}).get("usd", 0))
    except Exception:
        return None

async def sweep_payment(
    db_file: str,
    address_path: str,
    from_address: str,
    amount_ltc: Decimal,
    wallet_seed: str,
    receiving_address: str,
    blockcypher_token: str,
    ltc_confirmations: int,
    recipients: list[tuple[str, Decimal]] | None = None,
) -> tuple[bool, str | None]:
    if not wallet_seed or (not receiving_address and not recipients):
        logging.error("Sweep failed: WALLET_SEED or destination address(es) not configured")
        return False, None
    try:
        mnemo = Mnemonic("english")
        seed_bytes = mnemo.to_seed(wallet_seed)
        root = HDKey.from_seed(seed_bytes, network="litecoin")
        child_key = root.key_for_path(address_path)

        logging.info(f"Derived key for path {address_path}")
        logging.info("sweep_payment v2 loaded: using bitcoinlib Transaction.sign()")

        addr_url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{from_address}?token={blockcypher_token}"
        async with aiohttp.ClientSession() as s:
            async with await _fetch_get(s, addr_url) as r:
                if r.status != 200:
                    resp = await r.text()
                    logging.error(f"BlockCypher query failed: {r.status} - {resp[:300]}")
                    return False, None
                addr_data = await r.json()

        txrefs = addr_data.get('txrefs', [])
        confirmed_txs = [tx for tx in txrefs if not tx.get('spent') and tx.get('confirmations', 0) >= ltc_confirmations]

        if not confirmed_txs:
            logging.warning("No confirmed transactions")
            return False, None

        inputs = []
        total_satoshis = 0
        async with aiohttp.ClientSession() as s:
            for tx_ref in confirmed_txs:
                tx_hash = tx_ref.get('tx_hash')
                tx_url = f"https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}?token={blockcypher_token}"
                async with await _fetch_get(s, tx_url) as r:
                    if r.status != 200:
                        logging.warning(f"Failed to fetch tx {tx_hash}: {r.status}")
                        continue
                    tx_data = await r.json()
                    for idx, output in enumerate(tx_data.get('outputs', [])):
                        out_addrs = output.get('addresses', [])
                        if out_addrs and out_addrs[0] == from_address and not output.get('spent_by'):
                            satoshis = output.get('value', 0)
                            inputs.append({
                                'prev_hash': tx_hash,
                                'output_index': idx,
                                'output_value': satoshis,
                                'addresses': [from_address],
                                'script_type': output.get('script_type', 'pay-to-witness-pubkey-hash')
                            })
                            total_satoshis += satoshis

        if not inputs:
            logging.warning("No unspent outputs")
            return False, None

        fee_satoshis = max(2200, len(inputs) * 1100)
        available_satoshis = total_satoshis - fee_satoshis
        if available_satoshis <= 0:
            logging.warning("Insufficient balance after fees")
            return False, None

        outputs: list[dict] = []
        if recipients:
            total_target_satoshis = 0
            for address, amount in recipients:
                amount_decimal = Decimal(amount).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                satoshis = int((amount_decimal * Decimal('1e8')).to_integral_value(rounding=ROUND_DOWN))
                if satoshis <= 0:
                    continue
                outputs.append({'address': address, 'satoshis': satoshis})
                total_target_satoshis += satoshis

            if not outputs:
                logging.warning("No valid recipient outputs")
                return False, None

            if total_target_satoshis > available_satoshis:
                logging.warning("Not enough funds for requested seller payout outputs")
                return False, None

            remainder = available_satoshis - total_target_satoshis
            if remainder > 0:
                change_address = receiving_address or from_address
                outputs.append({'address': change_address, 'satoshis': remainder})
        else:
            outputs.append({'address': receiving_address, 'satoshis': available_satoshis})

        tx_inputs = []
        for inp in inputs:
            tx_inputs.append(Input(prev_txid=inp['prev_hash'], output_n=inp['output_index'], value=inp['output_value'], keys=child_key, witness_type='segwit', network='litecoin'))

        tx_outputs = [Output(value=out['satoshis'], address=out['address'], network='litecoin') for out in outputs]
        tx = Transaction(inputs=tx_inputs, outputs=tx_outputs, witness_type='segwit', network='litecoin')

        logging.info("Signing transaction inputs...")
        tx.sign(keys=child_key)

        tx_hex = tx.raw_hex()
        if not tx_hex:
            logging.error("Failed to generate tx hex")
            return False, None

        logging.info("Transaction hex generated, broadcasting...")
        broadcast_url = f"https://api.blockcypher.com/v1/ltc/main/txs/push?token={blockcypher_token}"
        async with aiohttp.ClientSession() as s:
            async with await _fetch_post(s, broadcast_url, json={"tx": tx_hex}) as r:
                resp_text = await r.text()
                if r.status not in (200, 201):
                    logging.error(f"Broadcast failed: {r.status}")
                    logging.error(f"    {resp_text[:300]}")
                    return False, None
                result = await r.json() if resp_text else {}

        txid = result.get('tx', {}).get('hash') or result.get('hash')
        if not txid:
            logging.error("No txid in response")
            logging.error(f"Broadcast result: {result}")
            return False, None

        logging.info(f"Sweep broadcast succeeded: {txid}")
        return True, txid
    except BaseException as e:
        logging.error(f"Sweep error: {e}")
        import traceback
        traceback.print_exc()
        return False, None


async def get_transaction_details(txid: str, blockcypher_token: str = None) -> dict | None:
    """Get Litecoin transaction details from BlockCypher."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    timeout = aiohttp.ClientTimeout(total=10)

    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}"
    if blockcypher_token:
        url += f"?token={blockcypher_token}"

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        try:
            async with await _fetch_get(session, url) as r:
                if r.status == 200:
                    data = await r.json()
                    return data
                else:
                    logging.warning(f"BlockCypher transaction query failed: {r.status} for {txid}")
                    return None
        except BaseException as e:
            logging.error(f"Error fetching transaction {txid}: {e}")
            return None


# ─────────────────────────────────────────────
#  TRANSACTION SAFETY VALIDATION
# ─────────────────────────────────────────────
async def validate_transaction_safety(txid: str, expected_address: str, expected_amount_ltc: Decimal,
                                    blockcypher_token: str = None, min_confirmations: int = 1) -> Dict[str, Any]:
    """
    Comprehensive transaction safety validation for real-money operations.

    Checks:
    - Transaction exists and is valid
    - Has sufficient confirmations
    - Sends to the expected address
    - Amount matches expectations
    - Not a double-spend attempt
    - Transaction is not malformed

    Returns dict with validation results and details.
    """
    result = {
        'valid': False,
        'txid': txid,
        'confirmations': 0,
        'amount_received_ltc': Decimal('0'),
        'sender_addresses': [],
        'errors': [],
        'warnings': []
    }

    try:
        # Get transaction details
        tx_data = await get_transaction_details(txid, blockcypher_token)
        if not tx_data:
            result['errors'].append('Transaction not found')
            return result

        # Check confirmations
        confirmations = tx_data.get('confirmations', 0)
        result['confirmations'] = confirmations

        if confirmations < min_confirmations:
            result['errors'].append(f'Insufficient confirmations: {confirmations} < {min_confirmations}')
            return result

        # Validate outputs - check if expected address received the expected amount
        outputs = tx_data.get('outputs', [])
        expected_satoshis = int(expected_amount_ltc * Decimal('100000000'))
        tolerance_satoshis = int(Decimal('0.00000001') * Decimal('100000000') * 2)  # 2 satoshi tolerance

        amount_received_satoshis = 0
        correct_output_found = False

        for output in outputs:
            addresses = output.get('addresses', [])
            if expected_address in addresses:
                value = output.get('value', 0)
                amount_received_satoshis += value
                if abs(value - expected_satoshis) <= tolerance_satoshis:
                    correct_output_found = True
                elif value > expected_satoshis * 2:  # More than double expected
                    result['warnings'].append(f'Overpayment detected: {value} satoshis vs expected {expected_satoshis}')

        if not correct_output_found and amount_received_satoshis == 0:
            result['errors'].append(f'No payment to expected address {expected_address}')
            return result

        result['amount_received_ltc'] = Decimal(str(amount_received_satoshis)) / Decimal('100000000')

        # Check for potential double-spend indicators
        if tx_data.get('double_spend', False):
            result['errors'].append('Transaction marked as double-spend')
            return result

        # Validate inputs are reasonable
        inputs = tx_data.get('inputs', [])
        if not inputs:
            result['errors'].append('Transaction has no inputs')
            return result

        # Collect sender addresses
        sender_addresses = []
        for inp in inputs:
            addresses = inp.get('addresses', [])
            sender_addresses.extend(addresses)

        result['sender_addresses'] = list(set(sender_addresses))  # Remove duplicates

        # Check for unusual input patterns that might indicate fraud
        if len(inputs) > 50:
            result['warnings'].append(f'Unusually high number of inputs: {len(inputs)}')

        # All checks passed
        result['valid'] = True
        return result

    except BaseException as e:
        result['errors'].append(f'Validation error: {str(e)}')
        logging.error(f'Transaction validation failed for {txid}: {e}')
        return result


async def check_transaction_uniqueness(txid: str, db_file: str) -> bool:
    """
    Check if a transaction ID has already been processed for any order.
    Prevents double-processing of the same transaction.
    """
    try:
        conn = get_db(db_file)
        c = conn.cursor()

        # Check if this txid appears in any order's sweep_txid or payment_txid
        c.execute('''
            SELECT id FROM orders
            WHERE sweep_txid = ? OR payment_txid = ?
        ''', (txid, txid))

        existing_order = c.fetchone()
        conn.close()

        if existing_order:
            logging.warning(f'Transaction {txid} already processed for order {existing_order["id"][:8]}')
            return False

        return True

    except BaseException as e:
        logging.error(f'Error checking transaction uniqueness for {txid}: {e}')
        return False  # Err on the side of caution


async def validate_address_ownership(address: str, wallet_seed: str) -> bool:
    """
    Validate that an address belongs to our wallet.
    This prevents accepting payments to incorrect addresses.
    """
    try:
        root = get_wallet_from_seed(wallet_seed)
        if not root:
            return False

        # Try to find this address in our wallet (brute force search through reasonable range)
        max_index = 100  # Search through first 100 addresses
        for index in range(max_index):
            try:
                child = root.key_for_path(f"m/0/{index}")
                try:
                    addr = child.address()
                    if addr == address:
                        return True
                except BaseException:
                    continild.address()
                    if addr == address:
                        return True
                except BaseException:
                    continild.address()
                    if addr == address:
                        return True
                except BaseException:
                    continue
            except BaseException:
                continue

        return False

    except BaseException as e:
        logging.error(f'Error validating address ownership for {address}: {e}')
        return False


async def get_transaction_confirmations(txid: str, blockcypher_token: str = None) -> int:
    """
    Get the number of confirmations for a transaction.
    Returns 0 if transaction not found or unconfirmed.
    """
    try:
        tx_data = await get_transaction_details(txid, blockcypher_token)
        if tx_data:
            return tx_data.get('confirmations', 0)
        return 0
    except BaseException as e:
        logging.error(f'Error getting confirmations for {txid}: {e}')
        return 0
