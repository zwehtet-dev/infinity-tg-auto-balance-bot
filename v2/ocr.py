"""Receipt OCR via OpenAI vision.

Wraps every model call with the reliability controls v1 lacked:

- a **semaphore** caps concurrent OCR calls (a burst of receipts can't stampede
  the API or exhaust rate limits),
- a **timeout** per attempt, with **retries + exponential backoff** on
  transient failures,
- one shared **JSON repair** path (code fences, comments, trailing commas,
  unquoted numeric keys) instead of five copies.

Prompts are carried over from v1 unchanged — they encode hard-won knowledge
about Myanmar bank receipt layouts and Binance/Swift receipt semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from openai import AsyncOpenAI

from config import Settings
from models import (
    BankBalance,
    BankMatchOcr,
    MmkOcrResult,
    UsdtReceivedOcr,
    UsdtSentOcr,
)

logger = logging.getLogger(__name__)


def _repair_json(raw: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from a model response."""
    text = re.sub(r"```json\s*|\s*```", "", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    text = text[start : end + 1]
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    text = re.sub(r'(\{|,)\s*(\d+)\s*:', r'\1"\2":', text)  # {1: 100} -> {"1": 100}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("OCR JSON repair failed: %.200s", text)
        return None


def _normalize_confidences(raw: dict, expected_ids: list[str]) -> dict[str, float]:
    confidences: dict[str, float] = {}
    for key, value in (raw or {}).items():
        key = str(key).strip()
        if not key.isdigit():
            continue
        try:
            confidences[key] = float(value or 0)
        except (TypeError, ValueError):
            confidences[key] = 0.0
    for bank_id in expected_ids:
        confidences.setdefault(bank_id, 0.0)
    return confidences


class OcrService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._semaphore = asyncio.Semaphore(settings.ocr_max_concurrency)

    async def _vision_json(self, prompt: str, image_base64: str, max_tokens: int = 400) -> Optional[dict]:
        """One vision call with concurrency cap, timeout, and retries."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self._settings.ocr_max_attempts + 1):
            try:
                async with self._semaphore:
                    response = await asyncio.wait_for(
                        self._client.chat.completions.create(
                            model=self._settings.ocr_model,
                            messages=[
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": "data:image/jpeg;base64," + image_base64
                                            },
                                        },
                                    ],
                                }
                            ],
                            max_tokens=max_tokens,
                            response_format={"type": "json_object"},
                        ),
                        timeout=self._settings.ocr_timeout_seconds,
                    )
                content = (response.choices[0].message.content or "").strip()
                data = _repair_json(content)
                if data is not None:
                    return data
                last_error = ValueError("unparseable OCR response")
            except asyncio.TimeoutError:
                last_error = TimeoutError(
                    f"OCR timed out after {self._settings.ocr_timeout_seconds}s"
                )
                logger.warning("OCR attempt %d timed out", attempt)
            except Exception as e:  # API/network errors
                last_error = e
                logger.warning("OCR attempt %d failed: %s", attempt, e)
            if attempt < self._settings.ocr_max_attempts:
                await asyncio.sleep(min(2.0 * attempt, 6.0))
        logger.error("OCR failed after %d attempts: %s", self._settings.ocr_max_attempts, last_error)
        return None

    # ------------------------------------------------------------- operations

    async def detect_mmk_bank_and_amount(
        self,
        image_base64: str,
        mmk_banks: list[BankBalance],
        user_prefix: Optional[str] = None,
    ) -> Optional[MmkOcrResult]:
        """Detect MMK bank + amount, optionally restricted to one staff prefix."""
        if user_prefix:
            candidates = [b for b in mmk_banks if b.prefix == user_prefix] or list(mmk_banks)
        else:
            candidates = list(mmk_banks)
        if not candidates:
            return None

        bank_list = ", ".join(f"{i + 1}. {b.bank_name}" for i, b in enumerate(candidates))
        prompt = f"""Analyze this MMK payment receipt carefully.

Available banks:
{bank_list}

Extract:
1. Transaction amount (integer, no decimals, positive number only)
2. Bank number (1-{len(candidates)})

CRITICAL - Bank Recognition Guide:
1. Kpay P: RED/CORAL color with "Payment Successful"
2. CB M: Blue "Account History" with "CB BANK" logo (Mobile)
3. CB: Rainbow "CB BANK" logo (Regular)
4. KBZ: "INTERNAL TRANSFER - CONFIRM" with green banner
5. AYA M: "AYA PAY" mobile app interface
6. AYA: "Payment Complete" OR "AYA PAY" logo (Regular)
7. AYA Wallet: "AYA Wallet" branding
8. Wave: YELLOW header with "Wave Money" logo (Regular Wave)
9. Wave M: Wave mobile app with "Wave Money" (Mobile)
10. Wave Channel: Green "Successful" with "Cash In" text and phone number display (Agent/Channel)
11. Yoma: "Flexi Everyday Account" text

IMPORTANT:
- "Wave Channel" shows "Cash In" with green checkmark and recipient phone number
- "Wave" shows yellow header with Wave Money logo
- "Wave M" shows Wave mobile app interface
- These are THREE DIFFERENT accounts - do not confuse them!

Return JSON:
{{"amount": <integer>, "bank_number": <1-{len(candidates)}>}}

CRITICAL NOTES:
1. Return amount as positive number, ignore any minus signs in the receipt
2. If you see "Cash In" with green checkmark and phone number → This is "Wave Channel" (NOT "Wave" or "Wave M")
3. Match the bank name EXACTLY as shown in the available banks list
4. Wave, Wave M, and Wave Channel are THREE DIFFERENT accounts"""

        data = await self._vision_json(prompt, image_base64, max_tokens=300)
        if not data:
            return None
        try:
            amount = abs(float(data["amount"]))
            bank_idx = int(data["bank_number"]) - 1
        except (KeyError, TypeError, ValueError):
            return None
        if 0 <= bank_idx < len(candidates):
            return MmkOcrResult(amount=amount, bank=candidates[bank_idx])
        return None

    async def extract_usdt_sent(self, image_base64: str) -> Optional[UsdtSentOcr]:
        """Staff sent USDT out: total to deduct = amount + network fee."""
        prompt = """Analyze this USDT transfer receipt from STAFF sending USDT to customer.

TASK: Extract the TOTAL USDT amount that was SPENT (amount sent + network fee).

We need to know how much to DEDUCT from our balance when staff sends USDT.

BANK TYPE IDENTIFICATION (CRITICAL):

1. SWIFT WALLET - Identify by these features:
   - Has "N" logo icon (blue/purple N symbol) at top
   - Clean white interface with purple/blue accents
   - Shows "Network fee" in TRX format (e.g., "8.4799 TRX (2.50 $)")
   - Has "View on block explorer" link at bottom
   - Shows Date, Status, Recipient fields
   - Recipient shows TRC20 address (starts with T...)
   - Amount displayed as "-X,XXX USDT" with USD equivalent below

2. BINANCE - Identify by these features:
   - Yellow/gold Binance logo or Binance branding
   - "Withdrawal Details" title at top
   - Text "Crypto transferred out of Binance" or similar
   - Shows Network (BSC, TRC20, etc.), Address, Txid, Amount, Network fee
   - Has "Withdraw Again" button (yellow) at bottom
   - Chinese version: 金额, 网络手续费
   - English version: Amount, Network fee, Withdrawal Wallet, Spot Wallet
   - Shows "Completed" status with green checkmark

3. WALLET (generic) - Use only if neither Swift nor Binance

RECEIPT STRUCTURE (Chinese Binance/Exchange):
- Main display: "-147.368 USDT" ← Amount customer receives
- 金额 (Amount): 148.368 USDT ← TOTAL we spent (this is what we need!)
- 网络手续费 (Network fee): 1 USDT

For SELL transactions, we need the TOTAL spent:
- total_amount = main_displayed_amount + network_fee
- OR total_amount = 金额 (Amount) field directly

EXAMPLES:

1. Binance Withdrawal Receipt (Chinese):
   - Main display: "-147.368 USDT" (customer receives)
   - 金额: 148.368 USDT (total we spent)
   - 网络手续费: 1 USDT
   Return: {"amount": 147.368, "network_fee": 1, "total_amount": 148.368, "bank_type": "binance"}

2. Binance Withdrawal Receipt (English):
   - "Withdrawal Details" title, "Crypto transferred out of Binance"
   - Main display: "-1,200 USDT" with green "Completed" checkmark
   - Network: BSC, Amount: 1,200 USDT, Network fee: 0 USDT
   Return: {"amount": 1200, "network_fee": 0, "total_amount": 1200, "bank_type": "binance"}

3. Swift Receipt (with N logo, TRX network fee, "View on block explorer"):
   - Shows: "-1,003 USDT" sent (with "1,001.72 $" below)
   - Network fee: 8.4799 TRX (2.50 $) ← Convert to USDT: ~2.50
   Return: {"amount": 1003, "network_fee": 2.50, "total_amount": 1005.50, "bank_type": "swift"}

4. Wallet Receipt:
   - Shows: "25.5 USDT" with no network fee
   Return: {"amount": 25.5, "network_fee": 0, "total_amount": 25.5, "bank_type": "wallet"}

RETURN EXACT JSON FORMAT:
{
    "amount": <the displayed/sent amount as positive number>,
    "network_fee": <network fee if shown, 0 if not - for Swift, use the USD value from TRX fee>,
    "total_amount": <amount + network_fee = total to deduct from balance>,
    "bank_type": "binance" or "swift" or "wallet"
}

CRITICAL RULES:
- If you see "N" logo + "Network fee" in TRX + "View on block explorer" → bank_type = "swift"
- For Swift receipts, network fee is shown in TRX with USD equivalent - use the USD value
- total_amount = amount + network_fee (ALWAYS add fee for all types)
- This is for SELL: we need TOTAL spent, not what customer receives
- Always return amounts as positive numbers
- bank_type must be "binance", "swift", or "wallet" (lowercase)"""

        data = await self._vision_json(prompt, image_base64, max_tokens=300)
        if not data:
            return None
        amount = abs(float(data.get("amount", 0) or 0))
        network_fee = abs(float(data.get("network_fee", 0) or 0))
        bank_type = data.get("bank_type")
        bank_type = bank_type.lower() if isinstance(bank_type, str) else "wallet"
        if bank_type not in ("swift", "wallet", "binance"):
            bank_type = "wallet"
        total = amount + network_fee
        provided_total = abs(float(data.get("total_amount", 0) or 0))
        if provided_total >= total:
            total = provided_total
        result = UsdtSentOcr(
            amount=amount, network_fee=network_fee, total_amount=total, bank_type=bank_type
        )
        logger.info("USDT sent OCR: %s", result)
        return result

    async def extract_usdt_received(self, image_base64: str) -> Optional[UsdtReceivedOcr]:
        """Customer sent USDT to us: the amount that lands in our wallet."""
        prompt = """Analyze this USDT transfer/withdrawal receipt from customer.

TASK: Extract the USDT amount that WE WILL RECEIVE (the final amount after network fee is deducted).

BANK TYPE IDENTIFICATION (CRITICAL):

1. SWIFT WALLET - Identify by these features:
   - Has "N" logo icon (blue/purple N symbol) at top
   - Clean white interface with purple/blue accents
   - Shows "Network fee" in TRX format (e.g., "8.4799 TRX (2.50 $)")
   - Has "View on block explorer" link at bottom
   - Recipient shows TRC20 address (starts with T...)

2. BINANCE - Identify by these features:
   - Yellow/gold Binance logo or Binance branding
   - "Withdrawal Details" title at top
   - Text "Crypto transferred out of Binance" or similar
   - Chinese version: 金额, 网络手续费
   - English version: Amount, Network fee, Withdrawal Wallet, Spot Wallet

3. WALLET (generic) - Use only if neither Swift nor Binance

CALCULATION RULES:
1. For English Binance: received_amount = Amount field - Network fee field
2. For Chinese Binance: received_amount = main displayed amount (already net of fees)
3. For Swift: received_amount = main displayed amount (already net of fees)

EXAMPLES:

1. English Binance Receipt:
   - Amount: 240.01 USDT, Network fee: 0.01 USDT
   - WE RECEIVE: 240.01 - 0.01 = 240 USDT
   → Return: {"received_amount": 240, "network_fee": 0.01, "bank_type": "binance"}

2. Chinese Binance Receipt:
   - Main display: "-147.368 USDT"
   - 金额: 148.368 USDT
   - 网络手续费: 1 USDT
   → Return: {"received_amount": 147.368, "network_fee": 1, "bank_type": "binance"}

3. Swift Receipt (with N logo, TRX network fee, "View on block explorer"):
   - Main display: "-1,003 USDT"
   - Network fee: 8.4799 TRX (2.50 $)
   → Return: {"received_amount": 1003, "network_fee": 2.50, "bank_type": "swift"}

RETURN JSON FORMAT:
{
    "received_amount": <the amount we actually receive after network fee deduction>,
    "network_fee": <network fee if shown, 0 if not - for Swift, use the USD value from TRX fee>,
    "bank_type": "binance" or "swift" or "wallet"
}

CRITICAL:
- Always return amounts as positive numbers
- bank_type must be "binance", "swift", or "wallet" (lowercase)"""

        data = await self._vision_json(prompt, image_base64, max_tokens=300)
        if not data:
            return None
        bank_type = data.get("bank_type")
        bank_type = bank_type.lower() if isinstance(bank_type, str) else "wallet"
        if bank_type not in ("swift", "wallet", "binance"):
            bank_type = "wallet"
        result = UsdtReceivedOcr(
            received_amount=abs(float(data.get("received_amount", 0) or 0)),
            network_fee=abs(float(data.get("network_fee", 0) or 0)),
            bank_type=bank_type,
        )
        logger.info("USDT received OCR: %s", result)
        return result

    async def match_mmk_receipt(
        self, image_base64: str, banks: list[dict]
    ) -> Optional[BankMatchOcr]:
        """Confidence-score a receipt against registered MMK accounts.

        ``banks``: [{'bank_id', 'bank_name', 'account_number', 'account_holder'}]
        """
        bank_info = "\n\n".join(
            f"Bank ID {b['bank_id']}: {b['bank_name']}\n"
            f"  Full Account: {b['account_number']}\n"
            f"  Account ends in: {b['account_number'][-4:] if len(b['account_number']) >= 4 else b['account_number']}\n"
            f"  Holder: {b['account_holder']}"
            for b in banks
        )
        prompt = f"""Analyze this MMK payment receipt and match it to the correct bank account.

REGISTERED BANK ACCOUNTS:
{bank_info}

BANK VISUAL IDENTIFICATION GUIDE:
- KBZ: "FAST TRANSFER - CONFIRM" header, green success banner, blue text
- CB Bank: Blue "CB BANK" logo, "Account History" header
- AYA: AYA Bank logo
- Yoma: Yoma Bank branding
- Kpay: RED/CORAL color with "Payment Successful", phone number format

TASK:
1. Extract the transaction amount (positive number, ignore minus signs)
2. Extract recipient/beneficiary account number (FULL number if visible, or partial if masked)
3. Extract recipient/beneficiary name
4. For EACH bank, calculate confidence score (0-100) based on:
   - Account number match: 60 points (check FULL account or last 4 digits)
   - Name match (case-insensitive, partial OK): 40 points

CRITICAL MATCHING RULES:
- FIRST check if the FULL account number is visible in the receipt
- If only partial account visible (e.g., "xxxx-xxxx-2957"), match last 4 digits
- Give 60 points for account match, 40 points for name match
- If no match at all, give 0 points

RETURN EXACT JSON FORMAT:
{{
    "amount": <number>,
    "banks": {{"<bank_id>": <confidence 0-100>, ...}}
}}

IMPORTANT:
- Return confidence for ALL banks in the list
- Use bank IDs exactly as provided (as strings)
- DO NOT include comments in JSON, DO NOT use trailing commas
- ONLY ONE bank should have high confidence (the matching one)"""

        data = await self._vision_json(prompt, image_base64, max_tokens=400)
        if not data:
            return None
        return BankMatchOcr(
            amount=abs(float(data.get("amount", 0) or 0)),
            confidences=_normalize_confidences(
                data.get("banks", {}), [str(b["bank_id"]) for b in banks]
            ),
        )

    async def match_usdt_receipt(
        self, image_base64: str, wallets: list[dict]
    ) -> Optional[BankMatchOcr]:
        """Confidence-score a receipt against registered USDT wallets.

        ``wallets``: [{'bank_id', 'bank_name', 'wallet_address', 'network'}]
        """
        wallet_info = "\n\n".join(
            f"Bank ID {w['bank_id']}: {w['bank_name']}\n"
            f"  Network: {w['network']}\n"
            f"  Full Address: {w['wallet_address']}\n"
            f"  Starts with: {w['wallet_address'][:6]}...\n"
            f"  Ends with: ...{w['wallet_address'][-6:]}"
            for w in wallets
        )
        prompt = f"""Analyze this USDT transfer receipt and match it to the correct receiving wallet.

REGISTERED USDT WALLETS:
{wallet_info}

NETWORK IDENTIFICATION GUIDE:
- BNB (BEP20): Binance Smart Chain, address starts with 0x, network fee ~$0.5
- ETH (ERC20): Ethereum, address starts with 0x, network fee >$1 (usually $2-10)
- Tron (TRC20): Tron network, address starts with T, network fee ~$1-2
- SOL: Solana network, base58 encoded address
- TON: TON network, address format UQ...

CRITICAL MATCHING RULES:
- Check recipient/destination wallet address in the receipt
- Match FULL address if visible, or last 6 characters if partially masked
- For BNB vs ETH (same address): Check network fee amount
  * If fee is ~$0.5 or less → BNB
  * If fee is >$1 (typically $2-10) → ETH
- Give 70 points for address match, 30 points for network match

RETURN EXACT JSON FORMAT:
{{
    "amount": <number>,
    "banks": {{"<bank_id>": <confidence 0-100>, ...}}
}}

IMPORTANT:
- Return confidence for ALL banks in the list
- Amount must be positive number
- Use bank IDs exactly as provided (as strings)
- DO NOT include comments in JSON, DO NOT use trailing commas
- ONLY ONE bank should have high confidence (the matching one)
- For 0x addresses with low fee (~$0.5), match to BNB not ETH"""

        data = await self._vision_json(prompt, image_base64, max_tokens=400)
        if not data:
            return None
        return BankMatchOcr(
            amount=abs(float(data.get("amount", 0) or 0)),
            confidences=_normalize_confidences(
                data.get("banks", {}), [str(w["bank_id"]) for w in wallets]
            ),
        )

    async def extract_amount(self, image_base64: str, currency_hint: str = "") -> Optional[float]:
        """Generic 'read the amount off this receipt' (internal transfers)."""
        hint = f" The amount is in {currency_hint}." if currency_hint else ""
        prompt = (
            f"Extract the transfer amount from this receipt.{hint}\n"
            'Return JSON: {"amount": <number>}\n'
            "Note: Return the amount as a positive number, ignore any minus signs."
        )
        data = await self._vision_json(prompt, image_base64, max_tokens=200)
        if not data:
            return None
        try:
            return abs(float(data["amount"]))
        except (KeyError, TypeError, ValueError):
            return None
