"""CLI smoke entry for the EDGAR adapter.

Usage:  python -m research_assistant.edgar <TICKER> <FORM>
Example: python -m research_assistant.edgar NVDA 10-K
"""
from __future__ import annotations

import asyncio
import json
import sys

from research_assistant.edgar.client import EdgarClient


async def _smoke_main(ticker: str, form: str) -> None:
    async with EdgarClient() as client:
        cik = await client.resolve_cik(ticker)
        if cik is None:
            print(f"No CIK for {ticker}")
            return
        filings = await client.list_filings(cik, form, limit=1)
        if not filings:
            print(f"No {form} filings for {ticker} (CIK {cik})")
            return
        f = filings[0]
        print(json.dumps({
            "ticker": ticker.upper(),
            "cik": cik,
            "form": f.form_type,
            "accession": f.accession_number,
            "filing_date": f.filing_date,
            "archive_url": f.archive_url,
        }, indent=2))
        text = await client.fetch_filing(f)
        print(f"\nParagraphs: {len(text.paragraphs)}")
        if text.paragraphs:
            print(f"First anchor: {text.anchor(0)}")
            print(f"First paragraph: {text.paragraphs[0][:300]}…")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m research_assistant.edgar <TICKER> <FORM>", file=sys.stderr)
        print("Example: python -m research_assistant.edgar NVDA 10-K", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_smoke_main(sys.argv[1], sys.argv[2]))
