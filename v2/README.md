# Infinity Balance Bot — v2

A ground-up rewrite of `bot.py` (6,400 lines, single file) into a modular,
reliable, auditable service. All v1 message formats, commands, topics and
workflows are preserved — the group does not need to change how it works.

## Running

```bash
cd v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in real values — never commit .env
python main.py
```

Works against the existing v1 database (SQLite or Postgres): v2 reuses all
v1 tables and only *adds* new ones (`balances`, `audit_log`,
`processed_messages`).

## Architecture

```
v2/
├── main.py            entrypoint: wiring, startup, maintenance loop
├── config.py          typed, validated Settings from env (fail-fast)
├── models.py          domain dataclasses (Balances, TxInfo, OCR results…)
├── db.py              async DB facade — worker threads, one dialect seam
├── repositories.py    all SQL: users, settings, banks, photos, OCR cache,
│                      audit log, idempotency claims
├── balances.py        balance parsing/formatting + BalanceLedger
├── parsing.py         pure-text transaction parsing (unit-testable)
├── ocr.py             OpenAI vision OCR: retries, timeout, concurrency cap
├── notify.py          outbound messages: routing, HTML-escaping, retries
├── media_groups.py    debounced album collector (replaces fixed sleeps)
├── services.py        dependency container + shared helpers
└── handlers/
    ├── router.py      message routing (topics, locations, dispatch)
    ├── transactions.py  Buy/Sell — one flow each, single photo or album
    ├── p2p.py         P2P sell (staff shorthand / breakdown / receipts)
    ├── internal.py    internal + coin transfers (Accounts Matter topic)
    ├── prescan.py     immediate OCR of sale messages (cached in DB)
    └── commands.py    slash commands + admin authorization
```

The v1 flow reference lives in the repo root: `botflow.md`.

## What improved, by concern

**Reliability / Data integrity**
- All balance mutations go through `BalanceLedger.apply()`: validate every
  leg first (bank exists, debits covered), then persist the new amounts *and*
  the audit record in **one DB transaction**, then update memory. A failure at
  any point leaves the sheet untouched — no more half-applied transfers.
- An `asyncio.Lock` serializes mutations. v1 let concurrent OCR tasks
  interleave read-modify-write on shared dicts.
- **Balances survive restarts** — persisted on every change, restored at
  startup. v1 kept them only in memory and forgot everything on restart.
- **Idempotency**: each settlement claims a `(message_id, action)` row before
  applying; redelivered Telegram updates or racing tasks can't double-apply
  a transaction. Claims are released on failure so retries still work.
- DB writes run off the event loop (`asyncio.to_thread`) with WAL mode for
  SQLite and a retry on transient errors. v1 performed blocking DB and file
  I/O directly on the event loop.

**Fault tolerance / Availability**
- OCR calls have a timeout, up to 3 attempts with backoff, and a concurrency
  cap — a hung OpenAI call no longer stalls a transaction forever.
- Outbound Telegram messages retry once; a failed notification is logged and
  dropped rather than crashing transaction processing.
- Media groups (albums) are collected with a **debounce** (flush after 2.5 s
  of quiet, hard cap 20 s) instead of v1's fixed `sleep(1.5)`/`sleep(8)` —
  faster on good networks, no lost photos on slow ones.
- The global error handler posts a clear "nothing was modified" notice to
  the group instead of failing silently.

**Security / Privacy / Compliance**
- `ADMIN_USER_IDS` allowlist: bank registration, user mapping and settings
  commands can be restricted. (Empty = v1-compatible open mode, logged.)
- Every user-controlled value (usernames, bank names, free text) is
  HTML-escaped before being embedded in `parse_mode='HTML'` messages —
  v1 was injectable.
- Secrets are only read from the environment, never logged; `.env.example`
  contains placeholders only.
- Configurable data retention: photos 24 h, OCR cache 48 h, audit rows 365
  days (see `.env.example`).

**Auditability**
- New `audit_log` table records every balance change: timestamp, transaction
  type, actor, source message id, description, and per-bank before/after
  deltas. Inspect from Telegram with `/audit [n]`.

**Performance / Scalability**
- Non-blocking DB & file I/O, bounded OCR concurrency, cached prescan results
  reused at settlement (no double OCR), `httpx` noise silenced in logs.

**Usability / UX**
- New commands: `/help`, `/health` (uptime, DB reachability, balance-loaded,
  config summary), `/audit`.
- Consistent, actionable error messages ("…add one with /set_usdt_bank
  first") instead of bare "❌ Error".
- Startup restores balances automatically — staff no longer need to re-post
  the balance sheet after every deploy.

**Compatibility**
- Same env vars (plus optional new ones), same commands, same message
  formats, same topic semantics, same balance-message format, same DB tables.
- v1 quirk fixed deliberately: default bank accounts are seeded only into an
  *empty* table, so deleted accounts no longer resurrect on restart.

## Message formats (unchanged from v1)

| Intent | Format |
|---|---|
| Balance sheet | `San(KBZ)-11044185 … USDT … THB …` |
| Buy / Sell sale | text containing `buy`/`sell`, amount, `= MMK`, + receipt photo(s) |
| Settlement | staff replies to the sale message with receipt photo(s); optional `fee-3039`, optional `From San(Kpay P)` |
| P2P sell (new) | `P2P Sell 1277.27×4148.30=5298500fee-0.12 [breakdown / photos]` |
| P2P sell (legacy) | `sell 13000000/3222.6=4034.00981 fee-6.44` |
| Staff P2P sell | `P2P Sell 440.18x4021 =1770000 … to OKM (KBZ) From OKM(Swift)` |
| Internal transfer | `San(Wave Channel) to NDT (Wave)` + receipt photo(s) |
| Coin transfer | `San (binance) to OKM(Wallet) 10 USDT-0.47 USDT(fee) = 9.53 USDT` |

## Operational notes

- **Rotate any credentials that were ever committed to git** (the v1
  `.env.example` at one point contained a real bot token and OpenAI key —
  treat both as compromised).
- Postgres is recommended in production (`DATABASE_URL`); SQLite (WAL mode)
  is fine for a single-group deployment.
- Amounts are floats end-to-end for v1 compatibility. If exactness at
  sub-cent scale ever matters, migrating the ledger to `Decimal` is the
  natural next step — the only write path is `BalanceLedger`.
