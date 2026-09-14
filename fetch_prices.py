#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page

BASE_URL = "https://www.amayama.com/en/part/{brand}/{part_number}"
PRICE_RE = re.compile(r"(?:^|\s)(\d[\d.,]*)\s*(?:EUR|€)?(?:\s|$)")


def normalize_number(value: str) -> float | None:
    value = value.strip().replace("\u00a0", " ")
    m = PRICE_RE.search(value)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


async def accept_cookies(page: Page) -> None:
    for label in ("Reject", "Accept"):
        button = page.get_by_role("button", name=label, exact=True)
        if await button.count():
            try:
                await button.first.click(timeout=1500)
                return
            except Exception:
                pass


async def set_select_option_by_text(page: Page, needle: str) -> bool:
    selects = page.locator("select")
    for i in range(await selects.count()):
        select = selects.nth(i)
        try:
            options = await select.locator("option").all_text_contents()
        except Exception:
            continue
        for idx, text in enumerate(options):
            if needle.lower() in text.strip().lower():
                try:
                    await select.select_option(index=idx)
                    await page.wait_for_timeout(1200)
                    return True
                except Exception:
                    pass
    return False


async def set_germany_eur(page: Page) -> None:
    # Amayama renders country/currency controls as selects on product pages.
    # We select by visible option text instead of relying on unstable CSS classes.
    germany_ok = await set_select_option_by_text(page, "Germany")
    eur_ok = await set_select_option_by_text(page, "EUR")

    if not germany_ok:
        raise RuntimeError("Could not set shipping country to Germany")
    if not eur_ok:
        raise RuntimeError("Could not set currency to EUR")

    await page.wait_for_timeout(1000)


async def verify_germany_eur(page: Page) -> None:
    body = await page.locator("body").inner_text()
    if "Germany" not in body:
        raise RuntimeError("Germany is not visible after country selection")

    eur_visible = "Price, EUR" in body or " EUR" in body or "€" in body
    if not eur_visible:
        raise RuntimeError("EUR is not visible after currency selection")


async def parse_offer_tables(page: Page) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    tables = page.locator("table")

    for ti in range(await tables.count()):
        table = tables.nth(ti)
        rows = table.locator("tr")
        if await rows.count() < 2:
            continue

        # Find a header-like row containing Price and determine column positions.
        price_idx = None
        source_idx = None
        header_row_idx = None
        for ri in range(min(await rows.count(), 4)):
            cells = rows.nth(ri).locator("th, td")
            texts = [t.strip() for t in await cells.all_text_contents()]
            for ci, text in enumerate(texts):
                low = text.lower()
                if price_idx is None and "price" in low:
                    price_idx = ci
                if source_idx is None and (low == "from" or "brand / from" in low):
                    source_idx = ci
            if price_idx is not None:
                header_row_idx = ri
                break

        if price_idx is None or header_row_idx is None:
            continue
        if source_idx is None:
            source_idx = 0

        for ri in range(header_row_idx + 1, await rows.count()):
            cells = rows.nth(ri).locator("td")
            count = await cells.count()
            if count <= price_idx or count <= source_idx:
                continue

            source = (await cells.nth(source_idx).inner_text()).strip()
            price_text = (await cells.nth(price_idx).inner_text()).strip()
            price = normalize_number(price_text)
            if price is None or not source:
                continue

            offers.append({
                "source": " ".join(source.split()),
                "price": price,
                "currency": "EUR",
            })

    # Keep all distinct visible offers but remove accidental duplicate DOM renderings.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, float, str]] = set()
    for offer in offers:
        key = (offer["source"], offer["price"], offer["currency"])
        if key not in seen:
            seen.add(key)
            deduped.append(offer)
    return deduped


async def fetch_part(page: Page, brand: str, part_number: str, first: bool) -> dict[str, Any]:
    url = BASE_URL.format(brand=brand, part_number=part_number)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await accept_cookies(page)

    if first:
        await set_germany_eur(page)
        # Reload to ensure all prices are rendered from the Germany/EUR session.
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await verify_germany_eur(page)

    body = await page.locator("body").inner_text()
    oop = 1 if "out of production" in body.lower() else 0
    prices = await parse_offer_tables(page)
    return {"OoP": oop, "prices": prices}


async def run(parts: list[str], brand: str, delay: float) -> dict[str, Any]:
    result: dict[str, Any] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        page = await context.new_page()

        for idx, part in enumerate(parts):
            try:
                result[part] = await fetch_part(page, brand, part, first=(idx == 0))
            except Exception as exc:
                result[part] = {
                    "OoP": 0,
                    "prices": [],
                    "error": str(exc),
                }
            if idx < len(parts) - 1:
                await asyncio.sleep(delay)

        await browser.close()
    return result


def read_parts(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch all Amayama prices for Germany in EUR")
    parser.add_argument("--input", default="parts.txt")
    parser.add_argument("--output", default="output.json")
    parser.add_argument("--brand", default=os.getenv("AMAYAMA_BRAND", "toyota"))
    parser.add_argument("--delay", type=float, default=float(os.getenv("AMAYAMA_DELAY", "4")))
    args = parser.parse_args()

    parts = read_parts(Path(args.input))
    data = asyncio.run(run(parts, args.brand, args.delay))
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
