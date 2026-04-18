import pytest
import sqlite3
import json
from unittest.mock import patch, MagicMock
import sys
import os

# Add the current directory to sys.path to import modules
sys.path.insert(0, os.path.dirname(__file__))

from shopbot.database import init_db, all_products, get_product, all_orders, reserve_stock_items, release_reserved_stock, get_reserved_stock_items_for_order
from shopbot.crypto import get_wallet_from_seed, get_address_balance, litoshi_to_ltc
from shopbot.shop import get_stock_status, notify_next_in_queue

class DummyResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

class DummySession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, timeout=None):
        return DummyResponse({"balance": 100000000, "unconfirmed_balance": 0}, status=200)

@pytest.fixture
def temp_db_path(tmp_path):
    """Create a temporary database file for testing."""
    db_file = tmp_path / "test.db"
    init_db(str(db_file))
    return str(db_file)

@pytest.fixture
def sample_config():
    """Sample config for testing."""
    return {
        "bot": {
            "token": "test_token",
            "prefix": "!",
            "admin_role": 987654321
        },
        "crypto": {
            "blockcypher_token": "test_bc_token",
            "wallet_seed": "test_seed",
            "receiving_address": "test_address",
            "payment_timeout": 3600,
            "poll_interval": 60,
            "ltc_confirmations": 1,
            "reservation_timeout": 300
        },
        "database": {
            "file": ":memory:"
        },
        "shop": {
            "guild_id": 123456789,
            "log_channel_id": 111111111
        }
    }

class TestDatabase:
    def test_init_db(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        conn.close()
        table_names = [table[0] for table in tables]
        assert "products" in table_names
        assert "orders" in table_names
        assert "stock_items" in table_names

    def test_get_product_none(self, temp_db_path):
        product = get_product(temp_db_path, "nonexistent")
        assert product is None

    def test_all_products_empty(self, temp_db_path):
        products = all_products(temp_db_path)
        assert products == []

    def test_all_orders_empty(self, temp_db_path):
        orders = all_orders(temp_db_path)
        assert orders == []

    def test_reserve_and_release_stock_items(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        c = conn.cursor()
        now = 1234567890.0
        c.execute("INSERT INTO products (id, name, description, price_ltc, stock, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  ("prod-1", "Test Product", "A sample product", 1.0, 0, now))
        c.execute("INSERT INTO stock_items (id, product_id, content, status, content_hash, created_at, restocked_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ("item-1", "prod-1", "code1", "pending", "hash1", now, "seller1"))
        c.execute("INSERT INTO stock_items (id, product_id, content, status, content_hash, created_at, restocked_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ("item-2", "prod-1", "code2", "pending", "hash2", now, "seller1"))
        conn.commit()
        conn.close()

        reserved = reserve_stock_items(temp_db_path, "prod-1", 1, "order-1")
        assert len(reserved) == 1
        assert reserved[0]["status"] == "reserved"
        assert reserved[0]["order_id"] == "order-1"

        released = release_reserved_stock(temp_db_path, "order-1")
        assert released == 1

        conn = sqlite3.connect(temp_db_path)
        c = conn.cursor()
        c.execute("SELECT status, order_id FROM stock_items WHERE id = ?", ("item-1",))
        row = c.fetchone()
        conn.close()
        assert row == ("pending", None)

    def test_reserve_stock_items_insufficient(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        c = conn.cursor()
        now = 1234567890.0
        c.execute("INSERT INTO products (id, name, description, price_ltc, stock, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  ("prod-2", "Test Product 2", "Another sample", 2.0, 0, now))
        c.execute("INSERT INTO stock_items (id, product_id, content, status, content_hash, created_at, restocked_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ("item-3", "prod-2", "code3", "pending", "hash3", now, "seller2"))
        conn.commit()
        conn.close()

        reserved = reserve_stock_items(temp_db_path, "prod-2", 2, "order-2")
        assert reserved == []

    def test_get_reserved_stock_items_for_order_includes_wallet(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        c = conn.cursor()
        now = 1234567890.0
        c.execute("INSERT INTO products (id, name, description, price_ltc, stock, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  ("prod-3", "Test Product 3", "Third sample", 3.0, 0, now))
        c.execute("INSERT INTO stock_items (id, product_id, content, status, content_hash, created_at, restocked_by, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  ("item-4", "prod-3", "code4", "reserved", "hash4", now, "seller3", "order-3"))
        c.execute("INSERT INTO user_wallets (user_id, ltc_address, linked_at, linked_by_admin, is_active) VALUES (?, ?, ?, ?, ?)",
                  ("seller3", "ltc1qtest", now, "admin", 1))
        conn.commit()
        conn.close()

        reserved_items = get_reserved_stock_items_for_order(temp_db_path, "order-3")
        assert len(reserved_items) == 1
        assert reserved_items[0]["ltc_address"] == "ltc1qtest"

class TestCrypto:
    def test_get_wallet_from_seed(self):
        wallet = get_wallet_from_seed("abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about")
        assert wallet is not None
        assert hasattr(wallet, 'address')

    def test_get_wallet_from_seed_invalid(self):
        wallet = get_wallet_from_seed("")
        assert wallet is None

    @pytest.mark.asyncio
    async def test_get_address_balance(self):
        with patch('shopbot.crypto.aiohttp.ClientSession', DummySession):
            balance = await get_address_balance("test_address", "test_token")
            assert balance["balance"] == 100000000

    def test_litoshi_to_ltc(self):
        ltc = litoshi_to_ltc(100000000)
        assert ltc == 1.0

class TestShop:
    def test_get_stock_status_no_product(self, temp_db_path):
        stock, emoji = get_stock_status(temp_db_path, "nonexistent")
        assert stock == 0
        assert emoji == "❌"

    @pytest.mark.asyncio
    async def test_notify_next_in_queue_no_queue(self, temp_db_path):
        # Mock bot
        mock_bot = MagicMock()
        
        # Should not raise error
        await notify_next_in_queue(temp_db_path, "test_product", mock_bot)
        # Since no queue, no calls should be made
        mock_bot.fetch_user.assert_not_called()