#!/usr/bin/env python3
"""
Pre-flight check for LIVE trading — run this BEFORE setting DRY_RUN=false.

It confirms, without placing any order or spending a cent:
  1. Which wallet the SDK will sign orders as (should be your funded proxy).
  2. That the proxy is deployed on-chain.
  3. That your API credentials match the order signer (the #1 cause of
     "order rejected" errors).
  4. That a real order can be built + signed locally (never posted).

Usage:
    cd /root/Miro2
    python scripts/check_live_wallet.py
"""
import asyncio
import json
import os
import sys

# Ensure repo root on path so "backend" imports work when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from backend.config import settings

RPC = "https://polygon-rpc.com"
GAMMA = "https://gamma-api.polymarket.com"


def _ok(msg):   print(f"  \033[32m✓\033[0m {msg}")
def _bad(msg):  print(f"  \033[31m✗\033[0m {msg}")
def _info(msg): print(f"  \033[36mi\033[0m {msg}")


async def _is_deployed(http, addr: str) -> bool:
    r = await http.post(RPC, json={
        "jsonrpc": "2.0", "method": "eth_getCode",
        "params": [addr, "latest"], "id": 1,
    })
    return r.json().get("result", "0x") not in ("0x", "")


async def _pick_token(http) -> str:
    r = await http.get(f"{GAMMA}/markets", params={
        "closed": "false", "active": "true", "limit": "10",
        "order": "volume24hr", "ascending": "false",
    })
    for m in r.json():
        ids = m.get("clobTokenIds")
        if ids:
            return json.loads(ids)[0]
    raise RuntimeError("no active market found")


async def main() -> int:
    print("\n=== Polymarket live-trading pre-flight ===\n")

    # ---- credentials present? -------------------------------------------
    missing = [k for k in (
        "polymarket_private_key", "polymarket_api_key",
        "polymarket_api_secret", "polymarket_api_passphrase",
    ) if not getattr(settings, k)]
    if missing:
        _bad(f"missing in .env: {', '.join(missing)}")
        return 1
    _ok("all four credentials present in .env")

    proxy_wallet = settings.polymarket_proxy_wallet
    if proxy_wallet:
        _ok(f"POLYMARKET_PROXY_WALLET = {proxy_wallet}")
    else:
        _info("POLYMARKET_PROXY_WALLET unset → SDK will derive the "
              "type-3 deposit wallet (only correct for bot-only accounts)")

    # Route SDK traffic through the configured proxy, if any.
    if settings.proxy_url:
        os.environ["HTTPS_PROXY"] = settings.proxy_url
        os.environ["HTTP_PROXY"] = settings.proxy_url
        _info(f"using outbound proxy {settings.proxy_url}")

    try:
        from polymarket import AsyncSecureClient, ApiKeyCreds
    except ImportError:
        _bad("polymarket SDK not installed: pip install --pre polymarket-client")
        return 1

    async with httpx.AsyncClient(timeout=20) as http:
        token_id = await _pick_token(http)

        # ---- build client, validating creds against the signer ----------
        try:
            client = await AsyncSecureClient._create(
                private_key=settings.polymarket_private_key,
                wallet=proxy_wallet or None,
                credentials=ApiKeyCreds(
                    key=settings.polymarket_api_key,
                    passphrase=settings.polymarket_api_passphrase,
                    secret=settings.polymarket_api_secret,
                ),
                validate_credentials=True,   # hits the API — confirms creds
            )
        except Exception as exc:
            _bad(f"client creation / credential validation failed: {exc}")
            _info("most often this means the API key was created for a "
                  "different wallet than the order signer")
            return 1
        _ok("API credentials validated against the CLOB")

        try:
            resolved = client._ctx.wallet
            _ok(f"SDK will sign orders as: {resolved}")
            if proxy_wallet and resolved.lower() != proxy_wallet.lower():
                _bad("resolved wallet does NOT match POLYMARKET_PROXY_WALLET")
                return 1

            # ---- on-chain deployment check ------------------------------
            if await _is_deployed(http, resolved):
                _ok("wallet is deployed on-chain")
            else:
                _bad("wallet is NOT deployed on-chain — it has never traded "
                     "and holds no funds; orders will be rejected")
                return 1

            # ---- build + sign a real order locally (NOT posted) ---------
            signed = await client.create_limit_order(
                token_id=token_id, price="0.50", size="1", side="BUY",
            )
            _ok("test order built + signed locally (not posted):")
            print(f"      signer         = {getattr(signed, 'signer', '?')}")
            print(f"      maker          = {getattr(signed, 'maker', '?')}")
            print(f"      signature_type = {getattr(signed, 'signature_type', '?')}")
        finally:
            await client.close()

    print("\n\033[32mAll checks passed.\033[0m You can set DRY_RUN=false to go live.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
