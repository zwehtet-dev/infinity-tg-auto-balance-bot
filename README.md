# Infinity Balance Bot

Clean, minimal Telegram bot for managing MMK and USDT balances independently with staff-specific tracking.

## Features

- ✅ **Staff-Specific Tracking**: Each staff member has their own bank accounts
- 🔍 **OCR Recognition**: Automatic bank detection from receipts using GPT-4 Vision
- 💱 **Buy/Sell Processing**: Handles both transaction types with staff attribution
- 💰 **MMK Fee Handling**: Staff can add fees to sell transactions (e.g., fee-3039)
- 📊 **Auto Balance Loading**: Reads balance from auto balance topic
- 🏦 **Multi-Bank Support**: CB, KBZ, Kpay, Kpay Partner, Wave, AYA, Yoma, Binance
- 🔄 **Internal Transfers**: Transfer between staff accounts in Accounts Matter topic
- 💸 **Coin Transfers**: USDT transfers with network fee handling (TRC20, BEP20, etc.)
- 💾 **SQLite Database**: Stores user-to-prefix mappings

## Setup

1. **Install dependencies**:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. **Configure environment**:
```bash
cp .env.example .env
# Edit .env with your credentials
```

3. **Run bot**:
```bash
python bot.py
```

## Deploy On Ubuntu VPS

Use the Ubuntu deployment guide in [DEPLOY_UBUNTU_VPS.md](DEPLOY_UBUNTU_VPS.md).

Included helper files:

- `deploy/infinity-balance-bot.service` - `systemd` unit for running the bot as a service

## Balance Message Format

New format with staff prefixes:
```
San(Kpay P) -2639565
San(CB M) -0
San(KBZ)-11044185
San(AYA M )-0
San(Wave) -0
San(Wave M )-1220723
San(Wave Channel) - 1970347
NDT (Wave) -2864900
MMM (Kpay p)-8839154

USDT
San(Swift) -81.99
MMN(Binance)-(15.86)
NDT(Binance)-6.96
TZT (Binance)-(222.6)
PPK (Binance) - 0
```

**Format:** `Prefix(BankName) -amount`
- **Prefix**: Staff identifier (San, TZT, MMN, NDT, etc.)
- **BankName**: Bank or wallet name
- **Amount**: Balance amount (MMK as integer, USDT with 2 decimals)

## Usage

### Initialize
1. Post balance message in auto balance topic
2. Bot auto-loads it
3. Set up staff mappings using `/set_user` command

### Set Up Staff Members
Reply to a staff member's message with:
```
/set_user San
/set_user TZT
/set_user MMN
/set_user NDT
```

### Buy Transaction (User buys USDT)
1. User posts: "Buy 100 = 235,000"
2. Staff replies with MMK receipt photo
3. Bot detects staff prefix and updates their specific bank
4. Posts updated balance to auto balance topic

### Sell Transaction (User sells USDT)
1. User posts: "Sell 100 = 235,000" with MMK receipt
2. Staff replies with USDT receipt photo
3. Bot updates staff's specific bank and USDT balance
4. Posts updated balance to auto balance topic

### Internal Transfer
In Accounts Matter topic:
```
San(Wave Channel) to NDT (Wave)
[attach receipt photo]
```
Bot detects amount, transfers between accounts, and updates balance

## Commands

- `/start` - Check bot status
- `/balance` - Show current balance
- `/load` - Load balance from message (reply to balance message)
- `/set_user <prefix>` - Set user prefix (reply to user's message)
- `/list_users` - List all user mappings
- `/set_mmk_bank` - Add/update MMK bank account
- `/edit_mmk_bank` - Edit existing MMK bank account
- `/remove_mmk_bank` - Remove MMK bank account
- `/list_mmk_bank` - List all MMK bank accounts
- `/list_usdt_banks` - List all USDT receiving wallets
- `/set_usdt_bank` - Add/update USDT receiving wallet
- `/edit_usdt_bank` - Edit existing USDT wallet
- `/remove_usdt_bank` - Remove USDT wallet
- `/set_receiving_usdt_acc` - Set default USDT receiving account (legacy)
- `/show_receiving_usdt_acc` - Show current USDT receiving account
- `/test` - Test connection and configuration

## Testing

Comprehensive testing documentation available:

- **[TESTING_CHECKLIST.md](TESTING_CHECKLIST.md)** - Complete testing checklist (57 tests)
- **[TEST_SCENARIOS.md](TEST_SCENARIOS.md)** - Detailed test scenarios with examples
- **[QUICK_TEST_GUIDE.md](QUICK_TEST_GUIDE.md)** - 15-minute quick test guide

**Automated Tests:**
```bash
python test_balance_parsing.py
python test_coin_transfer.py
python test_mmk_fee.py
```

**Quick Smoke Test (5 minutes):**
1. Start bot and load balance
2. Test buy transaction (with and without fee)
3. Test sell transaction (with and without fee)
4. Test coin transfer
5. Verify all commands work

## How It Works

1. **Balance Storage**: Balances stored as Telegram messages in auto balance topic
2. **User Mapping**: SQLite database stores user_id → prefix_name mappings
3. **OCR Processing**: GPT-4 Vision analyzes receipts to detect bank and amount
4. **Transaction Flow**:
   - Extract transaction info from message
   - Get staff prefix from database
   - OCR receipt(s) filtered by staff prefix
   - Verify amounts
   - Update staff-specific balance
   - Post new balance message
5. **Internal Transfers**: Detect transfer format, OCR amount, update both accounts

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TARGET_GROUP_ID` | Telegram group ID (negative number) |
| `USDT_TRANSFERS_TOPIC_ID` | Topic ID for transactions (set to 0 for main chat) |
| `AUTO_BALANCE_TOPIC_ID` | Topic ID for balance messages (set to 0 for main chat) |
| `ACCOUNTS_MATTER_TOPIC_ID` | Topic ID for internal transfers |
| `OPENAI_API_KEY` | OpenAI API key for GPT-4 Vision |

**Note:** If you don't use topics in your Telegram group, set topic IDs to `0` to use the main chat instead.

## Bank Recognition

The bot recognizes banks by visual features:
- **CB**: Blue "Account History" or rainbow logo
- **KBZ**: "INTERNAL TRANSFER - CONFIRM" with green banner
- **Kpay Partner**: RED/CORAL color with "Payment Successful"
- **Kpay**: BLUE with "KBZ Pay" branding
- **Wave**: YELLOW header or green "Successful"
- **AYA**: "Payment Complete" or "AYA PAY" logo
- **Yoma**: "Flexi Everyday Account" text
- **Binance**: Crypto exchange interface

## Staff Prefix Examples

Common staff prefixes used in the system:
- **San**: Main staff member
- **TZT**: Thin Zar Htet
- **MMN**: Staff member MMN
- **NDT**: Nandar
- **MMM**: Staff member MMM
- **PPK**: Staff member PPK

## Database

The bot uses SQLite (`bot_data.db`) to store:
- User ID to prefix name mappings
- Username for reference
- Creation timestamps

Database is automatically created on first run.

## New Features

### Multiple USDT Receiving Wallets
The bot now supports multiple USDT receiving wallets across different networks:
- **BNB (BEP20)**: Binance Smart Chain
- **ETH (ERC20)**: Ethereum network
- **Tron (TRC20)**: Tron network
- **SOL**: Solana network
- **TON**: TON network

When customers send USDT for buy transactions, the bot automatically:
1. Detects which wallet received the payment
2. Verifies the wallet address matches a registered wallet
3. Identifies the network type (BNB vs ETH, etc.)
4. Adds USDT to the correct wallet in the balance

**Commands:**
- `/list_usdt_banks` - View all registered wallets
- `/set_usdt_bank <name> | <address> | <network>` - Add/update wallet
- `/edit_usdt_bank <name> | <new_address> | <new_network>` - Edit wallet
- `/remove_usdt_bank <name>` - Remove wallet

**Example:**
```
/set_usdt_bank ACT(BNB) | 0x640e9AEde10B610834876cCc0ef2576C9469CB0e | BNB Wallet
```

### USDT Tolerance
- USDT transactions allow up to **0.03** difference for matching
- Accounts for small discrepancies in crypto transactions

### Multiple Receipt Support
- Send multiple photos as a media group for split payments
- Bot accumulates amounts from all receipts
- Verifies total matches expected amount

## Detailed Documentation

See [UPDATES.md](UPDATES.md) for:
- Complete feature documentation
- Setup instructions
- Usage examples
- Migration guide
- Troubleshooting

## License

MIT
