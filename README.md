# 🤖 Discord Shop Bot

A full-featured Discord shop bot with Litecoin (LTC) cryptocurrency payment integration via BlockCypher API. It enables server owners to create digital product stores within Discord, with automatic payment verification, stock management, order delivery, and seller payout systems. The bot uses SQLite for persistence and supports multi-seller scenarios with seller wallets and revenue tracking.

## **Tech Stack**
- **discord.py** (≥2.3.2) - Discord bot framework
- **aiohttp** (≥3.9.0) - HTTP client for async API calls
- **bitcoinlib** (≥0.6.14) - Litecoin wallet management
- **python-dotenv** (≥1.0.0) - Environment configuration
- **sqlite3** - Database
- **pytest** - Testing framework

---

## **Project Structure**

```
shopbot/           # Core business logic layer
├── database.py    # SQLite database operations
├── crypto.py      # LTC wallet and payment operations
└── shop.py        # Stock and product management

src/               # Application layer
├── bot.py         # Main Discord bot runner (v2.0)
├── commands/      # Command handlers
│   ├── handlers.py
│   └── __init__.py
├── services/      # Specialized services
│   ├── payment_engine.py
│   ├── order_manager.py
│   ├── stock_manager.py
│   └── tasks.py
├── database/      # DB wrappers
│   └── wrappers.py
└── tools/         # Utilities & scripts

ui/                # User Interface layer
├── embeds.py      # Discord embed builders
├── views.py       # Interactive UI views/buttons
└── modals.py      # Modal dialogs

utils/             # Global utilities
├── config.py      # Configuration loader
└── constants.py   # Constants and helpers

scripts/           # Maintenance utilities
├── check_db.py
├── check_orders.py
├── insert_order.py
└── insert.sql
```

---

## **Key Features & Functionalities**

### **User Commands**

| Command | Purpose | Type |
|---------|---------|------|
| `/shop`, `!shop` | Browse products with pagination/sorting | User |
| `/buy <id>`, `!buy <id>` | Purchase a product | User |
| `/order <id>`, `!order <id>` | Check specific order status | User |
| `/myorders`, `!myorders` | View user's order history | User |
| `/help`, `!help` | Show all available commands | User |
| `/ltc`, `!ltc` | Get current LTC/USD price | User |

### **Admin Commands (ShopAdmin role required)**

| Command | Purpose |
|---------|---------|
| `/addproduct`, `!addproduct <name> <price> <stock> <desc> \| <delivery>` | Create product |
| `/editproduct <id> <field> <value>` | Modify product details |
| `/deleteproduct <id>` | Remove product |
| `/restock <id>` | Add stock items to product |
| `/allorders` | View all orders globally |
| `/analytics` | Sales analytics |
| `/panel` | Admin control panel |
| `/seller_revenue <user>` | Check seller earnings |
| `/payouts <action>` | Process seller payouts |

---

## **Database Systems & Data Management**

### **Core Tables**

**products** - Product catalog
- Columns: id, name, description, category, price_ltc, price_usd, stock (unlimited if <0), delivery instructions, channel_id, embed_msg_id, embed_data, created_at/by, updated_at
- Supports categories and custom embed designs

**stock_items** - Physical stock inventory
- Columns: id, product_id, content (delivery details), status (pending/reserved/delivered), content_hash (duplicate detection), created_at, delivered_at/to, restocked_by
- Many-to-many: stock items can be reserved for orders and marked delivered

**orders** - Purchase records
- Columns: id, user_id, product_id, price_ltc, quantity, ltc_address, address_path, invoice_channel_id, invoice_message_id, status (pending/paid/delivered/canceled/expired/refunded), created_at, paid_at, swept_at, sweep_txid, refund_txid, refund_address, blockcypher_hook_id, last_payment_check
- Tracks payment detection, delivery, and refunds with retry counts

**audit_log** - Admin action tracking
- Columns: id, product_id, action, admin_id, admin_name, item_count, details, created_at
- Records restock, edit, and delete operations for compliance

**rate_limit** - User action throttling
- Columns: user_id, action, count, window_start
- Prevents abuse (e.g., rapid buy attempts)

**categories** - Product grouping
- Columns: id, name (unique), emoji, color
- For shop organization and filtering

**user_wallets** - Seller LTC addresses
- Columns: user_id (primary), ltc_address, linked_at, linked_by_admin, is_active
- Maps Discord user IDs to payment addresses for payouts

**sales_metrics** - Analytics (daily)
- Columns: date, total_revenue_ltc, total_orders, products_sold, unique_customers, top_product_id, created_at, updated_at
- For business intelligence

**payout_history** - Seller payment records
- Columns: id, seller_id, amount_ltc, platform_fee_percent, txid, status, processed_at, created_at

**performance_logs** - Operation monitoring
- Columns: operation, duration_ms, success, error_message, created_at
- For debugging and optimization

### **Database Optimization**
- **WAL mode** for better concurrency
- **Memory-mapped I/O** (256MB mmap_size)
- **64MB query cache**
- **Comprehensive indexes** on frequently-queried columns (status, created_at, product_id, user_id)
- **Automatic backups** (keeps last 7 days)
- **Connection retries** with exponential backoff
- **Database health monitoring** and maintenance

---

## **Payment & Crypto Handling**

### **Architecture**

```
User initiates /buy
    ↓
Bot generates unique LTC address via bitcoinlib (HD wallet)
    ↓
Registers BlockCypher webhook for tx-confirmation event
    ↓
Creates order with status: "pending"
    ↓
Sends payment invoice DM to user
    ↓
  ┌─────────────────────────────────────────┐
  │      DUAL PAYMENT DETECTION              │
  ├─────────────────────────────────────────┤
  │ 1. WEBHOOK (Primary - Real-time)        │
  │    BlockCypher pushes tx confirmation   │
  │                                         │
  │ 2. POLLING (Fallback - Every 5min)      │
  │    Checks pending orders if webhook     │
  │    fails, with adaptive rate limiting   │
  └─────────────────────────────────────────┘
    ↓
Payment detected & confirmed
    ↓
Sweep coins to main receiving address (or multi-seller wallets)
    ↓
Assign stock items → Mark order "delivered"
    ↓
DM user delivery content
```

### **Cryptocurrency Components**

**[shopbot/crypto.py](shopbot/crypto.py)** - Core LTC operations:
- **Wallet Management:**
  - HD wallet from BIP39 mnemonic seed (stored in .env as WALLET_SEED)
  - Derives unique address per order from m/0/{index} path
  - Supports recovery by path lookup

- **Address Generation:**
  - `generate_ltc_address()` - Creates unique address for each order
  - Stores address_path and address_index in order record
  - Prevents address reuse

- **Balance Checking:**
  - `get_address_balance()` - Fetches confirmed + unconfirmed balance
  - `get_addresses_balance()` - Batch check multiple addresses
  - 30-second result caching to prevent API spam
  - Supports BlockCypher and optional Blockchair fallback

- **Transaction Processing:**
  - `get_address_transactions()` - Fetches incoming txs from BlockCypher
  - Detects payment confirmations
  - Supports configurable confirmation requirements (default: 1 LTC confirmation)

- **Sweeping (Payment Collection):**
  - `sweep_payment()` - Consolidates order payment to main wallet
  - Supports multi-recipient output (seller payouts)
  - Retries up to 5 times with 5-minute backoff
  - Handles unconfirmed balance with notifications

- **Refund Processing:**
  - `process_automatic_refund()` - Auto-refunds canceled orders
  - Max 3 refund attempts per order
  - Validates address path before refunding

- **Webhook Integration:**
  - `register_blockcypher_webhook()` - Registers real-time payment notifications
  - `delete_blockcypher_webhook()` - Cleanup on order completion/cancel
  - Webhook validation with secret token

- **Rate Limiting:**
  - Rotating BlockCypher token support (up to 5 tokens via BLOCKCYPHER_TOKEN_1-5)
  - Token blacklisting on 429 rate-limit responses
  - 0.5s delay between API calls

### **Payment Flow Details**

1. **Order Creation:** Generates unique address, reserves stock, posts invoice
2. **Payment Detection:** Webhook (real-time) or polling (every 5min with adaptive delays)
3. **Confirmation Waiting:** Waits for 1 LTC confirmation (configurable)
4. **Sweep:** Transfers payment from order address to platform wallet
5. **Stock Delivery:** Assigns reserved stock items to order
6. **User Notification:** DMs user with delivery content + transaction ID
7. **Seller Payout:** (Optional) Sweeps seller's portion to their linked wallet

### **Configuration (config.json)**

```json
{
  "crypto": {
    "receiving_address": "ltc1qh5st...",  // Main platform wallet
    "payment_timeout": 3600,               // Order expires in 1 hour
    "ltc_confirmations": 1,               // Require 1 confirmation
    "poll_interval": 300,                 // Poll every 5 minutes
    "reservation_timeout": 300            // Stock reserved 5 mins
  }
}
```

---

## **Order Management**

**[src/services/order_manager.py](src/services/order_manager.py)** - Order lifecycle

**Order States:**
- `pending` - Awaiting payment
- `paid` - Payment confirmed, ready for delivery
- `delivered` - Stock assigned and user notified
- `canceled` - User cancelled, pending refund
- `expired` - Didn't pay within timeout
- `refunded` - Refund issued
- `failed` - Delivery/stock allocation failed
- `sweep_failed` - Payment collection failed

**Key Operations:**

1. **`process_buy()`** - Purchase initiation
   - Creates order record
   - Generates LTC address
   - Reserves stock items
   - Posts invoice message
   - Registers webhook
   - Sends user DM

2. **`update_invoice_message()`** - Real-time invoice updates
   - Shows current balance on address
   - Displays pending/confirmed transactions
   - Shows refund button if balance available
   - Disables buttons when order complete/expired

3. **`refresh_invoice_timers()`** - Background task
   - Updates expiration countdown every 60s
   - Refreshes pending payment status
   - Runs every INVOICE_REFRESH_INTERVAL seconds

4. **`deliver_order()`** - Post-payment delivery
   - Assigns reserved stock to order
   - Attempts sweep of payment
   - Creates payout records for sellers
   - DMs user delivery content
   - Notifies admins if out of stock
   - Triggers low-stock alerts

5. **`build_order_embed()`** - Order display formatting
   - Shows product name, quantity, unit price
   - Total LTC and USD values
   - Order creation time
   - Status indicator

6. **Order Cancellation:**
   - User initiates cancel (if pending)
   - Releases reserved stock
   - Auto-initiates refund if payment detected
   - Updates invoice to show cancel status

---

## **Stock Management**

**[src/services/stock_manager.py](src/services/stock_manager.py)** - Inventory operations

**Stock Item States:**
- `pending` - Available for purchase
- `staging` - Being added (in restock form)
- `reserved` - Allocated to specific order
- `delivered` - Sent to customer

**Key Functions:**

1. **`reserve_stock_items()`**
   - Atomic transaction: SELECT + UPDATE in single transaction
   - Prevents double-selling
   - Returns reserved items or fails if insufficient stock

2. **`assign_order_stock_to_order()`**
   - Matches pending stock to paid order
   - Creates seller payout records
   - Marks items as delivered
   - Updates delivery timestamps

3. **`get_stock_status()`**
   - Returns emoji indicator: 🟢 (>5), 🟡 (1-5), 🔴 (0), ∞ (unlimited)
   - Used for product embeds

4. **`update_stock_message()`**
   - Live-updates product embed with current stock count
   - Handles Discord rate limiting
   - Falls back to cache if fetch fails

5. **`send_low_stock_alert()`**
   - DMs admins when stock ≤ threshold
   - Shows restock command
   - Only triggers if stock actually low

6. **`notify_next_in_queue()`**
   - Notifies next queued user when stock becomes available
   - Updates queued order status
   - Allows next purchase to proceed

**Stock Features:**
- Duplicate detection via content hash to prevent re-selling
- Restocking workflow with staging status
- Seller attribution (who restocked which items)
- Audit trail of all stock changes
- Support for unlimited stock (stock < 0)

---

## **Bot Commands & Handlers**

**[src/commands/handlers.py](src/commands/handlers.py)** - 2100+ lines of command logic

**Command Architecture:**
- Dual support: slash commands (`/`) and prefix commands (`!`)
- Permission checks via roles
- Context-aware responses (DMs, channels, ephemeral)
- Error handling and user feedback

**User-Facing Commands:**

1. **Shop Browsing:**
   - `send_shop()` - Paginated product listing
   - Sorting: newest, price (low-to-high), popularity
   - Category filtering
   - Stock status indicators

2. **Purchasing:**
   - `process_buy()` - Initiate purchase
   - Validation: product exists, stock available, product enabled
   - Generates invoice message
   - Creates order record

3. **Order Status:**
   - `send_order_status()` - Check single order
   - Shows payment address, amount, status, time remaining
   - Displays transaction hash if paid
   - Cancel button if pending

4. **User Order History:**
   - `send_my_orders()` - All user's orders with status
   - Shows product, date, amount, status

**Admin Commands:**

1. **Product Management:**
   - `slash_addproduct()` - Create new product (modal form)
   - `slash_editproduct()` - Modify existing (name/price/description/delivery/stock)
   - `do_delete_product()` - Remove product
   - `update_product_fields()` - Field-by-field editing

2. **Stock Management:**
   - `prefix_restock()` / `slash_restock()` - Add stock items
   - Single/bulk restock (supports paste)
   - Duplicate detection
   - Rate limiting per seller

3. **Order Management:**
   - `send_all_orders()` - Global order list with filtering
   - `slash_insertorder()` - Manually create order (admin tool)
   - `do_cancel_order()` - Admin cancel + refund trigger

4. **Payment Operations:**
   - `slash_checkbalance()` - Query current balance on order address
   - `slash_sweep()` - Manually trigger payment sweep
   - `slash_checktxid()` - Look up transaction details
   - `slash_refund()` - Manually process refund

5. **Analytics & Monitoring:**
   - `analytics_command()` - Last 7 days sales metrics
   - `db_health_command()` - Database status
   - `send_audit_log()` - Product change history

6. **Seller Features:**
   - `slash_seller_revenue()` - Calculate seller earnings
   - `slash_payouts()` - Process/check seller payouts
   - Automatic payout on payment sweep

---

## **Configuration & Environment Setup**

**[utils/config.py](utils/config.py)** - Configuration management

**config.json Structure:**

```json
{
  "bot": {
    "prefix": "!",
    "admin_role": 1493058231210082326,
    "seller_role": 1493388810468331653,
    "invoice_channel_id": 1493058457232740382,
    "logging_channel_id": 1493058511486062735
  },
  "crypto": {
    "receiving_address": "ltc1qh5st48vcp6zazp8f9544s7pv0n6rlg60r32lje",
    "payment_timeout": 3600,
    "ltc_confirmations": 1,
    "poll_interval": 300,
    "reservation_timeout": 300
  },
  "database": {
    "file": "data/shop_data.db"
  },
  "shop": {
    "restock_rate_limit": 10,
    "low_stock_threshold": 5,
    "platform_fee_percent": 0.0
  },
  "payouts": {
    "enabled": false,
    "minimum_payout": 0.001,
    "auto_payout_threshold": 0.01,
    "schedule": "manual",
    "batch_size": 5
  }
}
```

**.env Variables Required:**

```
BOT_TOKEN=your_discord_bot_token
WALLET_SEED=your_bip39_mnemonic_seed_phrase
BLOCKCYPHER_TOKEN=your_single_token
# OR for rotating tokens:
BLOCKCYPHER_TOKEN_1=token1
BLOCKCYPHER_TOKEN_2=token2
...
BLOCKCYPHER_TOKEN_5=token5

# Optional:
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_BASE_URL=https://your-domain.com
WEBHOOK_SECRET=your_secret_for_webhook_validation
BLOCKCHAIR_API_KEY=fallback_balance_lookup
```

**Config Validation:**
- Loads and validates config.json on startup
- Checks required keys for each section
- Validates .env variables (BOT_TOKEN, WALLET_SEED)
- Checks for BlockCypher tokens (single or rotating)
- Exits with error if validation fails

---

## **Background Tasks & Services**

**[src/services/tasks.py](src/services/tasks.py)** - Scheduled operations

1. **`refresh_invoice_timers()`** (every 60s)
   - Updates expiration countdown on all pending invoices
   - Refreshes order status messages
   - Runs INVOICE_REFRESH_INTERVAL loop

2. **`check_payments()`** (every POLL_INTERVAL seconds - default 300s/5min)
   - Polls BlockCypher for pending order payments
   - Adaptive rate limiting: delays increase with order count
   - Skips recently-checked orders
   - Detects confirmed payments
   - Handles unconfirmed balance warnings
   - Triggers auto-refund for canceled orders
   - Processes payment delivery (sweep + assign stock)
   - Updates invoice messages with latest balance

3. **`update_analytics()`** (daily)
   - Aggregates daily sales metrics
   - Tracks revenue, order count, top products, unique customers
   - Stores in sales_metrics table

4. **`database_backup()`** (periodic)
   - Creates timestamped backup of database
   - Keeps last 7 days of backups
   - Cleans up old backups automatically

5. **`database_maintenance()`** (periodic)
   - Runs VACUUM/PRAGMA commands
   - Optimizes indexes
   - Validates database integrity

---

## **UI Components**

### **[ui/embeds.py](ui/embeds.py)** - Discord Embed Builders

1. **`build_invoice_embed()`**
   - Shows payment address (QR code format)
   - Amount due in LTC + USD
   - Current balance on address
   - Transaction list if payments detected
   - Time remaining
   - Platform fee info if applicable

2. **`build_live_embed()`**
   - Real-time product display
   - Stock status with emoji
   - Price in LTC and USD
   - Description and delivery instructions
   - Category and creation date
   - Custom color/branding

3. **`build_restock_embed()`**
   - Shows staging items count
   - Seller attribution
   - Audit history
   - Item preview

4. **`build_no_stock_embed()`**
   - Alert when product out of stock
   - Show when stock expected back
   - Suggest other products

5. **`build_seller_wallet_embed()`**
   - Seller's linked LTC address
   - Pending payouts
   - Payment history

### **[ui/views.py](ui/views.py)** - Interactive UI Elements

**Product Views:**
- `ProductDetailView` - Browse product + Buy button
- `ProductSelect` - Dropdown for product selection
- `DashboardView` - Admin dashboard with buttons
- `AdminPanelView` - Main admin control panel
- `RestockView` - Restock interface with staging

**Order Views:**
- `InvoiceApproveView` - Manual payment approve (admin)
- `OrderCancelView` - Cancel button on pending orders
- `RefundModal` - Manual refund interface

**Paginated Views:**
- `ShopPage` - Products pagination
- `StockItemPage` - Stock items pagination
- `PaginatedStockView` - Generic pagination

**Builder Views:**
- `EmbedBuilderView` - Product embed customization
- `start_product_builder()` - Wizard for new product

### **[ui/modals.py](ui/modals.py)** - Modal Dialogs

1. **Product Modals:**
   - `ProductCreateModal` - New product (name, price, stock, description)
   - `EditProductModal` - Modify product
   - `DeleteProductModal` - Confirm deletion
   - `RestockProductModal` - Add single stock item
   - `BulkRestockModal` - Paste multiple items

2. **Order Modals:**
   - `BuyProductModal` - Quantity selector for purchase
   - `OrderStatusModal` - Show order details

3. **Admin Modals:**
   - `AuditProductModal` - View product change history
   - `QuantityModal` - Quantity input
   - `ConfirmCancelModal` - Confirm order cancellation

4. **Utility Modals:**
   - `SingleFieldModal` - Generic text input
   - `ColorModal` - Hex color picker for embeds
   - `AddFieldModal` - Custom embed field
   - `RefundModal` - Refund parameters
   - `SetWalletModal` - Link seller wallet address

---

## **Scripts & Utilities**

**[scripts/](scripts/)** - Maintenance tools

1. **`check_db.py`** - Database inspection
   - Lists recent orders with status
   - Shows payment addresses and amounts
   - Quick health check

2. **`check_orders.py`** - Order query tool
   - Retrieve order details
   - Check payment status
   - Debug order issues

3. **`insert_order.py`** - Manual order insertion
   - Create orders programmatically
   - Bypass Discord for testing

4. **`insert.sql`** - SQL templates
   - Direct database queries
   - For advanced debugging

---

## **System Interactions & Data Flow**

### **Complete Purchase Flow**

```
USER: /buy product_id
  ↓
Command Handler checks:
  - Product exists
  - Has stock (pending items)
  - User role permissions
  ↓
reserve_stock_items() → Atomic DB transaction:
  - Locks stock_items table
  - Finds N pending items for product_id
  - Updates status to 'reserved'
  - Assigns to new order_id
  ↓
generate_ltc_address() → Derives unique address:
  - Gets next address_index from DB
  - Uses HDKey.subkey_for_path("m/0/{index}")
  - Returns: address, path, index
  ↓
Order created in DB:
  - status: 'pending'
  - price_ltc: calculated
  - ltc_address: newly generated
  - address_path: for later recovery
  - created_at: timestamp
  ↓
register_blockcypher_webhook():
  - POST to BlockCypher API
  - Registers tx-confirmation event
  - Stores hook_id in order record
  ↓
build_invoice_embed() creates rich embed:
  - LTC address (as QR reference)
  - Amount in LTC/USD
  - Expiration time
  - Current balance (0)
  ↓
Send to invoice_channel_id with:
  - InvoiceApproveView (approve/refund buttons)
  - Message ID stored in order record
  ↓
DM User payment details:
  - Send to user's DMs
  - Include address, amount, timeout
  - Link to invoice channel if applicable
  ↓
────── PAYMENT DETECTION ──────
  ↓
[Webhook Path] BlockCypher → POST /webhook:
  - Validates WEBHOOK_SECRET
  - Updates order: payment_detected_at = now()
  - Triggers immediate delivery
  ↓
[Polling Path] check_payments() every 5min:
  - Queries pending orders
  - Gets balance via get_address_balance()
  - If confirmed > 0:
    - Marks payment_detected_at
    - Queues for delivery
  ↓
process_payment_delivery():
  - Waits for LTC_CONFIRMATIONS (default: 1)
  - assign_order_stock_to_order():
    - SELECT reserved items for order
    - Mark as delivered
    - Create seller payout records
  ↓
sweep_payment() → Moves coins:
  - Constructs tx inputs from payment address
  - Creates outputs:
    * Platform receiving_address (full amount or after fee)
    * Seller wallets (if multi-seller, splits amount)
  - Broadcasts tx
  - Max 5 attempts with 5min retry
  - Stores sweep_txid in order record
  ↓
Update invoice message:
  - Changes status indicator
  - Disables approve button
  - Shows sweep transaction ID
  ↓
DM user delivery content:
  - Sends stock items' content field
  - Shows product name
  - Shows delivery timestamp
  - Shows transaction ID
  ↓
Update product embed:
  - Decrements stock count
  - Updates "in stock" display
  - Triggers low-stock alert if needed
  ↓
Order complete (status: 'delivered')
```

### **Admin Stock Restock Flow**

```
ADMIN/SELLER: /restock product_id
  ↓
RestockView buttons:
  - Add Single → RestockModal
  - Add Multiple → BulkRestockModal
  ↓
RestockModal submission:
  - Content input (delivery details)
  - content_hash = SHA256(content)
  ↓
Validation:
  - check_duplicate_stock(product_id, hash)
  - check_rate_limit(user_id, 'restock', limit=10)
  ↓
Create stock_item:
  - status: 'staging' (if seller) or 'pending' (if admin)
  - restocked_by: user_id
  - content_hash: for dedup
  - created_at: now
  ↓
If seller: "Staging" status (awaits admin approval)
If admin: "Pending" status (immediately available for sale)
  ↓
log_audit() → Records:
  - product_id, action: 'restock'
  - admin_id, admin_name
  - item_count
  ↓
View Staging button shows:
  - All items in 'staging'
  - Pagination if many items
  ↓
Done Restocking button:
  - Updates all staging → pending
  - Notifies if next in queue
  ↓
Stock now available for purchase
```

### **Seller Payout Flow**

```
PAYMENT SWEPT:
  ↓
assign_order_stock_to_order() processes:
  - Gets reserved_stock_items for order
  - For each item, gets seller's wallet address
  - build_seller_payout_from_pending_stock():
    * Calculates: price_per_item = price / quantity
    * Sums by seller wallet
    * Applies platform fee (if enabled)
    * Creates outputs: {seller_address: amount, ...}
  ↓
record_payout() stores in payout_history:
  - seller_id, amount_ltc
  - platform_fee_percent
  - status: 'completed' or 'pending'
  - processed_at: timestamp
  ↓
ADMIN: /payouts check
  - Lists pending payouts
  - Shows seller, amount, date
  ↓
ADMIN: /payouts process <seller_id>
  - process_seller_payout(seller_id):
    * Sums all unpaid from payout_history
    * Gets seller's linked wallet
    * Sweeps to seller wallet
    * Updates payout_history: status='sent', txid=...
  ↓
Seller receives payment on-chain
```

---

## **Dependencies & Requirements**

**Python Packages:**

| Package | Version | Purpose |
|---------|---------|---------|
| discord.py | ≥2.3.2 | Bot framework, slash commands, embeds, views |
| aiohttp | ≥3.9.0 | HTTP client for async API calls |
| bitcoinlib | ≥0.6.14 | HD wallet, Litecoin address generation, transactions |
| python-dotenv | ≥1.0.0 | Environment configuration |
| pytest | ≥7.0.0 | Testing framework |
| pytest-asyncio | ≥0.21.0 | Async test support |
| sqlite3 | Built-in | Database (SQLite) |

**External Services:**

| Service | Purpose | Fallback |
|---------|---------|----------|
| BlockCypher API | LTC balance, transactions, webhooks | Blockchair (optional) |
| Discord API | Bot commands, messages, embeds | - |
| LTC Blockchain | Transaction verification | - |

**Database Storage:**

| File | Purpose |
|------|---------|
| `data/shop_data.db` | Primary SQLite database |
| `data/bitcoinlib_wallet.db` | HD wallet keystore |
| `data/backups/shop_backup_*.db` | Daily backups (7 days) |

---

## **Key Architectural Patterns**

1. **Modular Layers:**
   - **Data Layer** (shopbot/) - Pure business logic
   - **Service Layer** (src/services/) - Orchestration and workflows
   - **Command Layer** (src/commands/) - Discord event handling
   - **UI Layer** (ui/) - Presentation and user interaction

2. **Async/Await Throughout:**
   - Fully asynchronous bot and payment processing
   - Non-blocking database operations
   - Parallel webhook and polling payment detection

3. **Database Transactions:**
   - Stock reservation uses `BEGIN IMMEDIATE` for atomicity
   - Prevents race conditions in concurrent orders

4. **Rate Limiting:**
   - User action throttling (restock limits)
   - BlockCypher token rotation
   - Adaptive polling delays based on order volume

5. **Error Recovery:**
   - Retry logic with exponential backoff
   - Webhook + polling dual detection for reliability
   - Automatic backup and maintenance

6. **Security:**
   - Webhook secret validation
   - Role-based access control (admin/seller)
   - HD wallet derivation for unique addresses per order
   - Input validation and sanitization

---

## **Quick Start**

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your tokens
```

### 3. Discord Bot Setup
1. Go to https://discord.com/developers/applications
2. Create a new application → Bot
3. Enable **Message Content Intent** and **Server Members Intent**
4. Copy the bot token into `.env`
5. Invite the bot with scopes: `bot` + `applications.commands`
   - Required permissions: Send Messages, Embed Links, Read Message History

### 4. BlockCypher (optional but recommended)
- Sign up free at https://accounts.blockcypher.com
- Free tier: 200 requests/hr without token, higher with token
- Add token to `.env`
- Optional: if BlockCypher becomes rate limited or blocked and you have a paid Blockchair API key, add `BLOCKCHAIR_API_KEY=...` to `.env` for a secondary Litecoin balance lookup

### 5. Admin Role
- Create a role named `ShopAdmin` in your Discord server (or change `ADMIN_ROLE` in `.env`)
- Assign it to trusted members

### 6. Run the bot
```bash
python bot.py
```

---

## **Summary**

This is a sophisticated, production-ready Discord commerce platform that seamlessly integrates Litecoin payments with inventory management. It handles complex multi-party transactions (buyers, sellers, platform), includes real-time payment detection with fallback polling, and provides comprehensive admin tools for store management. The architecture is modular, performant with database optimization, and resilient with automatic backups and retry logic.

---

## Database Storage

This bot now uses SQLite for persistent storage. The primary database file is `data/shop_data.db`, and it includes tables such as:

- `products`
- `orders`
- `stock_items`
- `audit_log`
- `rate_limit`
- `categories`
- `user_wallets`
- `sales_metrics`
- `performance_logs`
- `user_sessions`
- `payout_history`

SQLite offers safer writes, better concurrency, and faster queries than a JSON file store.

---

## Customisation

| Setting | Location | Default |
|---|---|---|
| Command prefix | `.env` → `PREFIX` | `!` |
| Admin role name | `.env` → `ADMIN_ROLE` | `ShopAdmin` |
| Payment timeout | `bot.py` → `PAYMENT_TIMEOUT` | `3600` (1hr) |
| Poll interval | `bot.py` → `POLL_INTERVAL` | `60` seconds |
| Min confirmations | `bot.py` → `LTC_CONFIRMATIONS` | `1` |

---

## Notes

- **Security**: Never commit your `.env` file. Add it to `.gitignore`.
- **Delivery**: The `delivery` field supports multi-line text — put license keys, download links, etc.
- **Stock**: Set `stock` to `-1` for unlimited. Any `0` value shows as "Out of Stock".
- **Slash sync**: Slash commands sync on startup. If they don't appear, wait up to 1 hour for Discord to propagate, or call `/sync` in a test server first.
