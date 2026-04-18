import os
import sqlite3
import logging
import time
import shutil
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple
from functools import wraps

# ─────────────────────────────────────────────
#  DATABASE CONNECTION MANAGEMENT
# ─────────────────────────────────────────────

def get_db_connection(db_file: str):
    """Get a new database connection."""
    db_file = os.path.abspath(db_file)

    conn = sqlite3.connect(db_file, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
    conn.execute("PRAGMA synchronous=NORMAL")  # Balance performance/safety
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")  # Temp tables in memory
    conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory mapping

    return conn

def close_db_connections():
    """Placeholder for cleanup when using persistent connection caches."""
    return

# ─────────────────────────────────────────────
#  DATABASE BACKUP SYSTEM
# ─────────────────────────────────────────────
def create_database_backup(db_file: str, backup_dir: str = "data/backups") -> str:
    """Create a timestamped backup of the database"""
    try:
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"shop_backup_{timestamp}.db")

        # Close any cached connections before backup
        close_db_connections()

        # Create backup
        shutil.copy2(db_file, backup_file)

        # Cleanup old backups (keep last 7 days)
        cleanup_old_backups(backup_dir, days_to_keep=7)

        logging.info(f"Database backup created: {backup_file}")
        return backup_file

    except Exception as e:
        logging.error(f"Failed to create database backup: {e}")
        return None

def cleanup_old_backups(backup_dir: str, days_to_keep: int = 7):
    """Remove backups older than specified days"""
    try:
        if not os.path.exists(backup_dir):
            return

        cutoff_date = datetime.now() - timedelta(days=days_to_keep)

        for filename in os.listdir(backup_dir):
            if not filename.startswith("shop_backup_") or not filename.endswith(".db"):
                continue

            filepath = os.path.join(backup_dir, filename)
            file_date = datetime.fromtimestamp(os.path.getctime(filepath))

            if file_date < cutoff_date:
                os.remove(filepath)
                logging.info(f"Removed old backup: {filename}")

    except Exception as e:
        logging.error(f"Failed to cleanup old backups: {e}")

# ─────────────────────────────────────────────
#  ERROR HANDLING DECORATOR
# ─────────────────────────────────────────────
def db_operation_retry(max_retries: int = 3, backoff_factor: float = 0.1):
    """Decorator for database operations with retry logic"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                    last_exception = e

                    if attempt < max_retries - 1:
                        wait_time = backoff_factor * (2 ** attempt)  # Exponential backoff
                        logging.warning(f"Database operation failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time:.2f}s...")
                        time.sleep(wait_time)
                    else:
                        logging.error(f"Database operation failed after {max_retries} attempts: {e}")
                        raise e
                except Exception as e:
                    # Don't retry for non-database errors
                    raise e

            raise last_exception
        return wrapper
    return decorator

# ─────────────────────────────────────────────
#  DATABASE INITIALIZATION
# ─────────────────────────────────────────────
@db_operation_retry()
def init_db(db_file: str):
    db_file = os.path.abspath(db_file)
    db_dir = os.path.dirname(db_file)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = get_db_connection(db_file)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        category TEXT DEFAULT 'General',
        price_ltc REAL NOT NULL,
        price_usd REAL,
        stock INTEGER DEFAULT 0,
        delivery TEXT,
        channel_id INTEGER,
        embed_msg_id INTEGER,
        created_at REAL,
        created_by TEXT,
        updated_at REAL,
        embed_data TEXT
    )''')

    existing_columns = [row[1] for row in c.execute("PRAGMA table_info(products)").fetchall()]
    if "delivery" not in existing_columns:
        c.execute("ALTER TABLE products ADD COLUMN delivery TEXT")
    if "embed_data" not in existing_columns:
        c.execute("ALTER TABLE products ADD COLUMN embed_data TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS stock_items (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        content_hash TEXT,
        created_at REAL,
        delivered_at REAL,
        delivered_to TEXT,
        restocked_by TEXT,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        price_ltc REAL NOT NULL,
        ltc_address TEXT,
        address_path TEXT,
        address_index INTEGER DEFAULT 0,
        invoice_channel_id TEXT,
        invoice_message_id INTEGER,
        channel_id INTEGER,
        message_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at REAL,
        paid_at REAL,
        swept_at REAL,
        sweep_txid TEXT,
        sweep_attempts INTEGER DEFAULT 0,
        last_sweep_attempt REAL,
        notified_unconfirmed INTEGER DEFAULT 0,
        last_payment_check REAL,
        delivered_at REAL,
        refund_txid TEXT,
        refund_address TEXT,
        refund_at REAL,
        refund_attempts INTEGER DEFAULT 0,
        blockcypher_hook_id TEXT,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        product_id TEXT,
        action TEXT,
        admin_id TEXT,
        admin_name TEXT,
        item_count INTEGER,
        details TEXT,
        created_at REAL,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS rate_limit (
        user_id TEXT,
        action TEXT,
        count INTEGER DEFAULT 1,
        window_start REAL,
        PRIMARY KEY (user_id, action)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        emoji TEXT,
        color INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_wallets (
        user_id TEXT PRIMARY KEY,
        ltc_address TEXT NOT NULL,
        linked_at REAL,
        linked_by_admin TEXT,
        is_active INTEGER DEFAULT 1
    )''')

    migrations = [
        'ALTER TABLE orders ADD COLUMN address_path TEXT',
        'ALTER TABLE orders ADD COLUMN address_index INTEGER DEFAULT 0',
        'ALTER TABLE orders ADD COLUMN invoice_channel_id TEXT',
        'ALTER TABLE orders ADD COLUMN invoice_message_id INTEGER',
        'ALTER TABLE orders ADD COLUMN channel_id INTEGER',
        'ALTER TABLE orders ADD COLUMN message_id INTEGER',
        'ALTER TABLE orders ADD COLUMN swept_at REAL',
        'ALTER TABLE orders ADD COLUMN sweep_attempts INTEGER DEFAULT 0',
        'ALTER TABLE orders ADD COLUMN notified_unconfirmed INTEGER DEFAULT 0',
        'ALTER TABLE orders ADD COLUMN last_sweep_attempt REAL',
        'ALTER TABLE orders ADD COLUMN last_payment_check REAL',
        'ALTER TABLE orders ADD COLUMN payment_detected_at REAL',
        'ALTER TABLE orders ADD COLUMN sweep_txid TEXT',
        'ALTER TABLE orders ADD COLUMN refund_txid TEXT',
        'ALTER TABLE orders ADD COLUMN refund_address TEXT',
        'ALTER TABLE orders ADD COLUMN refund_at REAL',
        'ALTER TABLE orders ADD COLUMN refund_attempts INTEGER DEFAULT 0',
        'ALTER TABLE orders ADD COLUMN blockcypher_hook_id TEXT',
        'ALTER TABLE orders ADD COLUMN quantity INTEGER DEFAULT 1',
        'ALTER TABLE stock_items ADD COLUMN restocked_by TEXT',
        'ALTER TABLE stock_items ADD COLUMN order_id TEXT',
        'ALTER TABLE stock_items ADD COLUMN message_channel_id INTEGER',
        'ALTER TABLE stock_items ADD COLUMN message_id INTEGER'
    ]

    for sql in migrations:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass

    # ─────────────────────────────────────────────
    #  PERFORMANCE INDEXES (CRITICAL FOR SCALE)
    # ─────────────────────────────────────────────
    indexes = [
        # Orders table - most queried
        'CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_orders_product_status ON orders(product_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC)',

        # Stock items - frequently queried for availability
        'CREATE INDEX IF NOT EXISTS idx_stock_product_status ON stock_items(product_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_stock_status_created ON stock_items(status, created_at ASC)',
        'CREATE INDEX IF NOT EXISTS idx_stock_content_hash ON stock_items(content_hash)',

        # Products - for browsing and search
        'CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)',
        'CREATE INDEX IF NOT EXISTS idx_products_created_at ON products(created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_products_channel ON products(channel_id)',

        # Audit log - for admin tracking
        'CREATE INDEX IF NOT EXISTS idx_audit_product_action ON audit_log(product_id, action)',
        'CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC)',

        # Rate limiting - performance critical
        'CREATE INDEX IF NOT EXISTS idx_rate_limit_user_action ON rate_limit(user_id, action)',

        # Categories - for product organization
        'CREATE INDEX IF NOT EXISTS idx_categories_name ON categories(name)',
    ]

    for index_sql in indexes:
        try:
            c.execute(index_sql)
        except sqlite3.OperationalError as e:
            logging.warning(f"Could not create index: {e}")

    # ─────────────────────────────────────────────
    #  ANALYTICS TABLES (FOR SCALE INSIGHTS)
    # ─────────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS sales_metrics (
        id TEXT PRIMARY KEY,
        date TEXT NOT NULL, -- YYYY-MM-DD
        total_revenue_ltc REAL DEFAULT 0,
        total_orders INTEGER DEFAULT 0,
        products_sold INTEGER DEFAULT 0,
        unique_customers INTEGER DEFAULT 0,
        top_product_id TEXT,
        created_at REAL,
        updated_at REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS performance_logs (
        id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        duration_ms INTEGER,
        success BOOLEAN,
        error_message TEXT,
        created_at REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        session_start REAL,
        session_end REAL,
        actions_count INTEGER DEFAULT 0,
        products_viewed TEXT, -- JSON array
        orders_placed INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS payout_history (
        id TEXT PRIMARY KEY,
        seller_id TEXT NOT NULL,
        amount_ltc REAL NOT NULL,
        platform_fee_percent REAL DEFAULT 0.0,
        txid TEXT,
        status TEXT DEFAULT 'pending',
        processed_at REAL,
        created_at REAL,
        FOREIGN KEY (seller_id) REFERENCES user_wallets(user_id)
    )''')

    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
#  DATABASE HELPERS
# ─────────────────────────────────────────────
@db_operation_retry()
def get_db(db_file: str):
    """Get database connection with optimizations"""
    return get_db_connection(db_file)

def get_product(db_file: str, product_id: str) -> Optional[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM products WHERE id = ?', (product_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def all_products(db_file: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM products ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_order(db_file: str, order_id: str) -> Optional[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def all_products_by_category(db_file: str, category: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM products WHERE category = ? ORDER BY created_at DESC', (category,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_categories(db_file: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM categories')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def add_category(db_file: str, name: str, emoji: str = "📦", color: int = 0x9B59B6) -> str:
    import uuid
    cat_id = str(uuid.uuid4())[:8]
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('INSERT INTO categories (id, name, emoji, color) VALUES (?, ?, ?, ?)',
              (cat_id, name, emoji, color))
    conn.commit()
    conn.close()
    return cat_id

def all_orders(db_file: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM orders ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_stock_items(db_file: str, product_id: str, status: str = None) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    if status:
        c.execute('SELECT * FROM stock_items WHERE product_id = ? AND status = ? ORDER BY created_at ASC',
                  (product_id, status))
    else:
        c.execute('SELECT * FROM stock_items WHERE product_id = ? ORDER BY created_at ASC', (product_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def reserve_stock_items(db_file: str, product_id: str, quantity: int, order_id: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    conn.execute('BEGIN IMMEDIATE')
    c.execute('SELECT id FROM stock_items WHERE product_id = ? AND status = ? ORDER BY created_at ASC LIMIT ?',
              (product_id, 'pending', quantity))
    rows = c.fetchall()
    if len(rows) < quantity:
        conn.rollback()
        conn.close()
        return []

    item_ids = [row['id'] for row in rows]
    c.executemany('UPDATE stock_items SET status = ?, order_id = ? WHERE id = ?',
                  [('reserved', order_id, item_id) for item_id in item_ids])
    conn.commit()

    c.execute('SELECT * FROM stock_items WHERE order_id = ? ORDER BY created_at ASC', (order_id,))
    reserved_rows = c.fetchall()
    conn.close()
    return [dict(row) for row in reserved_rows]


def release_reserved_stock(db_file: str, order_id: str) -> int:
    conn = get_db(db_file)
    c = conn.cursor()
    conn.execute('BEGIN IMMEDIATE')
    c.execute('UPDATE stock_items SET status = ?, order_id = NULL WHERE order_id = ? AND status = ?',
              ('pending', order_id, 'reserved'))
    released = c.rowcount
    conn.commit()
    conn.close()
    return released


def get_reserved_stock_items_for_order(db_file: str, order_id: str) -> List[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('''
        SELECT si.*, uw.ltc_address
        FROM stock_items si
        LEFT JOIN user_wallets uw ON si.restocked_by = uw.user_id AND uw.is_active = 1
        WHERE si.order_id = ? AND si.status IN ('reserved', 'delivered')
        ORDER BY si.created_at ASC
    ''', (order_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def build_seller_payout_from_pending_stock(db_file: str, order: dict, platform_fee_percent: float = 0.0, receiving_address: str | None = None) -> tuple[list[tuple[str, Decimal]] | None, str | None]:
    quantity = int(order.get('quantity', 1))
    conn = get_db(db_file)
    c = conn.cursor()

    # Get pending stock items for this product with seller info
    c.execute('''
        SELECT si.id, si.restocked_by, uw.ltc_address
        FROM stock_items si
        LEFT JOIN user_wallets uw ON si.restocked_by = uw.user_id AND uw.is_active = 1
        WHERE si.product_id = ? AND si.status = 'pending'
        ORDER BY si.created_at ASC LIMIT ?
    ''', (order['product_id'], quantity))
    pending_items = c.fetchall()
    conn.close()

    if len(pending_items) != quantity:
        return None, f"Not enough pending stock ({len(pending_items)} available, {quantity} required)."

    price_per_item = Decimal(str(order['price_ltc'])) / Decimal(str(quantity))
    recipients: dict[str, Decimal] = {}
    for item in pending_items:
        item_dict = dict(item)
        seller_wallet = item_dict.get('ltc_address')
        if not seller_wallet:
            return None, "One or more sellers for available stock are missing a linked wallet."
        recipients[seller_wallet] = recipients.get(seller_wallet, Decimal('0')) + price_per_item

    fee_percent = Decimal(str(platform_fee_percent))
    if fee_percent and fee_percent > 0 and receiving_address:
        fee_multiplier = fee_percent / Decimal('100')
        fee_total = sum(recipients.values()) * fee_multiplier
        for address, amount in list(recipients.items()):
            recipients[address] = (amount * (Decimal('1') - fee_multiplier)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        recipients[receiving_address] = recipients.get(receiving_address, Decimal('0')) + fee_total.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

    return [(address, amount) for address, amount in recipients.items()], None


def assign_order_stock_to_order(db_file: str, order: dict) -> bool:
    quantity = int(order.get('quantity', 1))
    conn = get_db(db_file)
    c = conn.cursor()

    # Use immediate transaction lock to prevent race conditions
    conn.execute('BEGIN IMMEDIATE')

    try:
        c.execute('SELECT COUNT(*) FROM stock_items WHERE order_id = ? AND status IN ("reserved", "delivered")', (order['id'],))
        already_assigned = c.fetchone()[0]
        if already_assigned >= quantity:
            conn.rollback()
            conn.close()
            return True

        c.execute('''SELECT id FROM stock_items
                     WHERE product_id = ? AND status = 'pending'
                     ORDER BY created_at ASC LIMIT ?''', (order['product_id'], quantity - already_assigned))
        pending_rows = c.fetchall()
        if len(pending_rows) < (quantity - already_assigned):
            conn.rollback()
            conn.close()
            return False

        now = datetime.now(timezone.utc).timestamp()
        updates = [
            (now, order['user_id'], order['id'], row[0])
            for row in pending_rows
        ]
        c.executemany('''UPDATE stock_items SET status = 'delivered', delivered_at = ?, delivered_to = ?, order_id = ?
                         WHERE id = ?''', updates)
        c.execute('''UPDATE products
                     SET stock = (SELECT COUNT(*) FROM stock_items WHERE product_id = ? AND status = 'pending')
                     WHERE id = ?''', (order['product_id'], order['product_id']))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.rollback()
        conn.close()
        logging.error(f"Error assigning stock to order {order['id']}: {e}")
        return False


def get_user_wallet(db_file: str, user_id: str) -> Optional[dict]:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT * FROM user_wallets WHERE user_id = ? AND is_active = 1', (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def set_user_wallet(db_file: str, user_id: str, ltc_address: str, linked_by_admin: str) -> bool:
    from datetime import datetime, timezone
    conn = get_db(db_file)
    c = conn.cursor()
    now = datetime.now(timezone.utc).timestamp()
    
    # Check if address is already linked to another user
    c.execute('SELECT user_id FROM user_wallets WHERE ltc_address = ? AND is_active = 1', (ltc_address,))
    existing = c.fetchone()
    if existing and existing['user_id'] != user_id:
        conn.close()
        return False  # Address already linked to another user
    
    # Insert or update wallet
    c.execute('''INSERT OR REPLACE INTO user_wallets 
                 (user_id, ltc_address, linked_at, linked_by_admin, is_active)
                 VALUES (?, ?, ?, ?, 1)''',
              (user_id, ltc_address, now, linked_by_admin))
    conn.commit()
    conn.close()
    return True

def remove_user_wallet(db_file: str, user_id: str) -> bool:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('UPDATE user_wallets SET is_active = 0 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    return True

def get_seller_revenue(db_file: str, seller_id: str, start_date: float = None, end_date: float = None, platform_fee_percent: float = 0.0) -> dict:
    """Calculate revenue for a seller based on items they restocked that were sold"""
    conn = get_db(db_file)
    c = conn.cursor()
    
    # Get all delivered stock items by this seller
    query = '''
        SELECT si.product_id, COUNT(*) as items_sold, 
               SUM(CASE WHEN o.quantity > 1 THEN (o.price_ltc / o.quantity) * (1 - ?) ELSE o.price_ltc * (1 - ?) END) as revenue
        FROM stock_items si
        JOIN orders o ON si.product_id = o.product_id
        WHERE si.restocked_by = ? AND si.status = 'delivered' AND o.status = 'delivered'
    '''
    fee_multiplier = platform_fee_percent / 100.0
    params = [fee_multiplier, fee_multiplier, seller_id]
    
    if start_date:
        query += ' AND o.delivered_at >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND o.delivered_at <= ?'
        params.append(end_date)
    
    query += ' GROUP BY si.product_id'
    
    c.execute(query, params)
    results = c.fetchall()
    
    total_revenue = 0
    total_items = 0
    product_breakdown = []
    
    for row in results:
        revenue = row['revenue'] or 0
        items = row['items_sold'] or 0
        total_revenue += revenue
        total_items += items
        product_breakdown.append({
            'product_id': row['product_id'],
            'items_sold': items,
            'revenue': revenue
        })
    
    conn.close()
    return {
        'total_revenue': total_revenue,
        'total_items': total_items,
        'product_breakdown': product_breakdown
    }

def record_payout(db_file: str, seller_id: str, amount_ltc: float, platform_fee_percent: float, txid: str = None, status: str = 'completed') -> str:
    """Record a payout in history"""
    import uuid
    from datetime import datetime, timezone
    payout_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).timestamp()
    
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('''INSERT INTO payout_history 
                 (id, seller_id, amount_ltc, platform_fee_percent, txid, status, processed_at, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (payout_id, seller_id, amount_ltc, platform_fee_percent, txid, status, now, now))
    conn.commit()
    conn.close()
    return payout_id

def get_payout_history(db_file: str, seller_id: str = None, limit: int = 50) -> List[dict]:
    """Get payout history"""
    conn = get_db(db_file)
    c = conn.cursor()
    if seller_id:
        c.execute('SELECT * FROM payout_history WHERE seller_id = ? ORDER BY created_at DESC LIMIT ?',
                  (seller_id, limit))
    else:
        c.execute('SELECT * FROM payout_history ORDER BY created_at DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def log_audit(db_file: str, product_id: str, action: str, admin_id: str, admin_name: str,
              item_count: int = 0, details: str = "") -> str:
    import uuid
    from datetime import datetime, timezone
    log_id = str(uuid.uuid4())[:8]
    conn = get_db(db_file)
    c = conn.cursor()
    now = datetime.now(timezone.utc).timestamp()
    try:
        c.execute('''INSERT INTO audit_log
                     (id, product_id, action, admin_id, admin_name, item_count, details, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (log_id, product_id, action, admin_id, admin_name, item_count, details, now))
    except sqlite3.OperationalError as e:
        if 'no such table: audit_log' in str(e):
            conn.close()
            init_db(db_file)
            conn = get_db(db_file)
            c = conn.cursor()
            c.execute('''INSERT INTO audit_log
                         (id, product_id, action, admin_id, admin_name, item_count, details, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (log_id, product_id, action, admin_id, admin_name, item_count, details, now))
        else:
            conn.close()
            raise
    conn.commit()
    conn.close()
    return log_id

def check_rate_limit(db_file: str, user_id: str, action: str, limit: int, window_seconds: int = 60) -> bool:
    from datetime import datetime, timezone
    conn = get_db(db_file)
    c = conn.cursor()
    now = datetime.now(timezone.utc).timestamp()

    c.execute('SELECT count, window_start FROM rate_limit WHERE user_id = ? AND action = ?',
              (user_id, action))
    row = c.fetchone()

    if not row:
        c.execute('INSERT INTO rate_limit (user_id, action, count, window_start) VALUES (?, ?, 1, ?)',
                  (user_id, action, now))
        conn.commit()
        conn.close()
        return True

    count, window_start = row['count'], row['window_start']
    if now - window_start > window_seconds:
        c.execute('UPDATE rate_limit SET count = 1, window_start = ? WHERE user_id = ? AND action = ?',
                  (now, user_id, action))
        conn.commit()
        conn.close()
        return True

    if count >= limit:
        conn.close()
        return False

    c.execute('UPDATE rate_limit SET count = count + 1 WHERE user_id = ? AND action = ?',
              (user_id, action))
    conn.commit()
    conn.close()
    return True

def hash_content(content: str) -> str:
    import hashlib
    return hashlib.md5(content.strip().encode()).hexdigest()

def check_duplicate_stock(db_file: str, product_id: str, content_hash: str) -> bool:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT id FROM stock_items WHERE product_id = ? AND content_hash = ?',
              (product_id, content_hash))
    result = c.fetchone()
    conn.close()
    return result is not None

def get_next_address_index(db_file: str) -> int:
    conn = get_db(db_file)
    c = conn.cursor()
    c.execute('SELECT MAX(address_index) FROM orders')
    row = c.fetchone()
    conn.close()
    if row and row[0] is not None:
        return int(row[0]) + 1
    return 0

# ─────────────────────────────────────────────
#  PERFORMANCE MONITORING
# ─────────────────────────────────────────────
@db_operation_retry()
def log_performance(operation: str, duration_ms: float, success: bool, error_message: str = None):
    """Log performance metrics for monitoring"""
    import uuid
    from datetime import datetime, timezone

    log_id = str(uuid.uuid4())[:8]
    created_at = datetime.now(timezone.utc).timestamp()

    # Only log if operation took more than 100ms or failed
    if duration_ms < 100 and success:
        return

    try:
        conn = get_db(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO performance_logs
                     (id, operation, duration_ms, success, error_message, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (log_id, operation, int(duration_ms), success, error_message, created_at))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to log performance: {e}")

# ─────────────────────────────────────────────
#  ANALYTICS FUNCTIONS
# ─────────────────────────────────────────────
@db_operation_retry()
def update_daily_sales_metrics(db_file: str):
    """Update sales metrics for today"""
    import uuid
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).timestamp()

    conn = get_db(db_file)
    c = conn.cursor()

    # Calculate today's metrics
    c.execute('''
        SELECT
            COUNT(*) as total_orders,
            COUNT(DISTINCT user_id) as unique_customers,
            COALESCE(SUM(price_ltc), 0) as total_revenue,
            COUNT(CASE WHEN status = 'delivered' THEN 1 END) as products_sold
        FROM orders
        WHERE DATE(created_at, 'unixepoch') = ?
    ''', (today,))

    metrics = c.fetchone()

    # Find top product
    c.execute('''
        SELECT product_id, COUNT(*) as order_count
        FROM orders
        WHERE DATE(created_at, 'unixepoch') = ?
        GROUP BY product_id
        ORDER BY order_count DESC
        LIMIT 1
    ''', (today,))

    top_product = c.fetchone()
    top_product_id = top_product['product_id'] if top_product else None

    # Update or insert metrics
    metrics_id = f"metrics_{today}"
    c.execute('''
        INSERT OR REPLACE INTO sales_metrics
        (id, date, total_revenue_ltc, total_orders, products_sold, unique_customers, top_product_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        metrics_id, today,
        metrics['total_revenue'], metrics['total_orders'],
        metrics['products_sold'], metrics['unique_customers'],
        top_product_id, now, now
    ))

    conn.commit()
    conn.close()

@db_operation_retry()
def get_sales_analytics(db_file: str, days: int = 30) -> Dict:
    """Get sales analytics for the last N days"""
    conn = get_db(db_file)
    c = conn.cursor()

    # Get daily metrics
    c.execute('''
        SELECT date, total_revenue_ltc AS total_revenue, total_orders, products_sold, unique_customers
        FROM sales_metrics
        WHERE date >= DATE('now', '-{} days')
        ORDER BY date DESC
    '''.format(days))

    daily_data = c.fetchall()

    # Get overall totals
    c.execute('''
        SELECT
            SUM(total_revenue_ltc) as total_revenue,
            SUM(total_orders) as total_orders,
            SUM(products_sold) as total_sold,
            SUM(unique_customers) as total_customers
        FROM sales_metrics
        WHERE date >= DATE('now', '-{} days')
    '''.format(days))

    totals = c.fetchone()
    conn.close()

    return {
        'daily_metrics': [dict(row) for row in daily_data],
        'totals': dict(totals) if totals else {},
        'period_days': days
    }

@db_operation_retry()
def get_database_health(db_file: str) -> Dict:
    """Get database health and performance metrics"""
    conn = get_db(db_file)
    c = conn.cursor()

    health = {}

    # Table sizes
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = c.fetchall()

    for table in tables:
        table_name = table['name']
        c.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = c.fetchone()[0]
        health[f'{table_name}_count'] = count

    # Database file size
    db_size = os.path.getsize(db_file)
    health['database_size_mb'] = round(db_size / (1024 * 1024), 2)

    # Performance metrics (last 24 hours)
    c.execute('''
        SELECT
            operation,
            AVG(duration_ms) as avg_duration,
            MAX(duration_ms) as max_duration,
            COUNT(*) as total_calls,
            SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures
        FROM performance_logs
        WHERE created_at >= strftime('%s', 'now', '-1 day')
        GROUP BY operation
        ORDER BY avg_duration DESC
    ''')

    perf_data = c.fetchall()
    health['performance_metrics'] = [dict(row) for row in perf_data]

    conn.close()
    return health

# ─────────────────────────────────────────────
#  UTILITY FUNCTIONS
# ─────────────────────────────────────────────
def optimize_database(db_file: str):
    """Optimize database performance"""
    try:
        conn = get_db_connection(db_file)
        c = conn.cursor()

        # Run optimization commands
        c.execute("VACUUM")  # Reclaim space
        c.execute("ANALYZE")  # Update query planner statistics
        c.execute("PRAGMA optimize")  # SQLite optimization

        conn.commit()
        conn.close()

        logging.info("Database optimization completed")
        return True

    except Exception as e:
        logging.error(f"Database optimization failed: {e}")
        return False