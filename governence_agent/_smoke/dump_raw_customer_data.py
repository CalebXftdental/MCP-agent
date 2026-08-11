"""Dump RAW customer order-summary data (no ratio/status filtering applied) --
exactly what a chat LLM would see as tool results if asked the user's plain-
English win-back question. Used to test whether LLM reasoning over this data
matches the deterministic cadence-ratio math, without pre-computing anything.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).parent.parent.resolve()
GATEWAY_URL = "https://governence-agent-dycufrgya2ewash8.canadaeast-01.azurewebsites.net/mcp"
API_KEY = (ROOT / "mcp-knowledge" / ".env.local").read_text(encoding="utf-8")
API_KEY = next(line.split("=", 1)[1].strip() for line in API_KEY.splitlines() if line.strip().startswith("local_admin_api_key"))


async def main() -> None:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with streamablehttp_client(GATEWAY_URL, headers=headers, timeout=60) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool, args):
                result = await session.call_tool(tool, arguments={**args, "session_id": "raw-dump-test"})
                for part in result.content or []:
                    if getattr(part, "text", None) is not None:
                        return json.loads(part.text)
                return {}

            region = await call("minierp_accounts_get_customers_by_region", {"country": "US", "page": 1, "page_size": 25})
            customers = region.get("customers") or []
            raw = []
            for c in customers[:15]:
                cid = c.get("customerId")
                if not cid:
                    continue
                summary = await call("minierp_orders_get_customer_order_summary", {"customer_id": cid, "start_date": "", "end_date": ""})
                if summary.get("status") != "ok":
                    continue
                raw.append({
                    "name": c.get("name"), "customerId": cid, "status": c.get("status"),
                    "orderCount": summary.get("orderCount"),
                    "firstOrderDate": summary.get("firstOrderDate"),
                    "lastOrderDate": summary.get("lastOrderDate"),
                    "grandTotal": summary.get("grandTotal"),
                })
            (ROOT / "_smoke" / ".tmp" / "raw-customer-data.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
            print(json.dumps(raw, indent=2))


asyncio.run(main())
