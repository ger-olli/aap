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


async def select_native_option(page: Page, needle: str) -> bool:
    selects = page.locator("select")
    for i in range(await selects.count()):
        select = selects.nth(i)
        options = select.locator("option")
        for j in range(await options.count()):
            text = (await options.nth(j).inner_text()).strip()
            if needle.lower() in text.lower():
                value = await options.nth(j).get_attribute("value")
                if value is not None:
                    try:
                        await select.select_option(value=value)
                        await page.wait_for_timeout(1000)
                        return True
                    except Exception:
                        pass
    return False


async def click_first_visible_text(page: Page, text: str, exact: bool = True) -> bool:
    loc = page.get_by_text(text, exact=exact)
    for i in range(await loc.count()):
        item = loc.nth(i)
        try:
            if await item.is_visible():
                await item.click(timeout=3000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


async def set_country_germany(page: Page) -> None:
    body = await page.locator("body").inner_text()
    if re.search(r"Shipping to\s+Germany", body, re.I):
        return

    # Native select, if Amayama exposes one in this layout.
    if await select_native_option(page, "Germany"):
        await page.wait_for_timeout(1200)
        body = await page.locator("body").inner_text()
        if re.search(r"Shipping to\s+Germany", body, re.I):
            return

    # Current destination is rendered as clickable text/custom control on product pages.
    m = re.search(r"Shipping to\s+([^\n\[]+)", body, re.I)
    current = m.group(1).strip() if m else None
    opened = False
    if current:
        opened = await click_first_visible_text(page, current, exact=True)
    if not opened:
        # Fallback to a visible shipping selector/combobox.
        combos = page.get_by_role("combobox")
        for i in range(await combos.count()):
            try:
                if await combos.nth(i).is_visible():
                    await combos.nth(i).click(timeout=2500)
                    opened = True
                    break
            except Exception:
                pass

    if opened:
        if await click_first_visible_text(page, "Germany", exact=True):
            await page.wait_for_timeout(1500)
            body = await page.locator("body").inner_text()
            if re.search(r"Shipping to\s+Germany", body, re.I):
                return

    raise RuntimeError("Could not set shipping country to Germany")


async def set_currency_eur(page: Page) -> None:
    body = await page.locator("body").inner_text()
    if re.search(r"Price,\s*EUR", body, re.I):
        return

    if await select_native_option(page, "EUR"):
        await page.wait_for_timeout(1000)
        body = await page.locator("body").inner_text()
        if re.search(r"Price,\s*EUR", body, re.I):
            return

    # Amayama exposes currency choices (USD/AUD/.../EUR) as clickable text in headers.
    eur = page.get_by_text("EUR", exact=True)
    for i in range(await eur.count()):
        item = eur.nth(i)
        try:
            if await item.is_visible():
                await item.click(timeout=3000)
                await page.wait_for_timeout(1200)
                body = await page.locator("body").inner_text()
                if re.search(r"Price,\s*EUR", body, re.I):
                    return
        except Exception:
            continue

    raise RuntimeError("Could not set currency to EUR")


async def set_germany_eur(page: Page) -> None:
    await set_country_germany(page)
    await set_currency_eur(page)


async def verify_germany_eur(page: Page) -> None:
    body = await page.locator("body").inner_text()
    if not re.search(r"Shipping to\s+Germany", body, re.I):
        raise RuntimeError("Shipping destination is not Germany")
    if not re.search(r"Price,\s*EUR", body, re.I):
        raise RuntimeError("Displayed price currency is not EUR")


async def parse_offer_tables(page: Page) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    tables = page.locator("table")
    for ti in range(await tables.count()):
        rows = tables.nth(ti).locator("tr")
        if await rows.count() < 2:
            continue
        price_idx = source_idx = header_row_idx = None
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
            price = normalize_number((await cells.nth(price_idx).inner_text()).strip())
            if price is None or not source:
                continue
            offers.append({"source": " ".join(source.split()), "price": price, "currency": "EUR"})
    deduped = []
    seen = set()
    for offer in offers:
        key = (offer["source"], offer["price"], offer["currency"])
        if key not in seen:
            seen.add(key)
            deduped.append(offer)
    return deduped


async def fetch_part(page: Page, brand: str, part_number: str, first: bool) -> dict[str, Any]:
    await page.goto(BASE_URL.format(brand=brand, part_number=part_number), wait_until="domcontentloaded", timeout=60000)
    await accept_cookies(page)
    if first:
        await set_germany_eur(page)
        await page.wait_for_timeout(1000)
    await verify_germany_eur(page)
    body = (await page.locator("body").inner_text()).lower()
    oop = 1 if "out of production" in body else 0
    return {"OoP": oop, "prices": await parse_offer_tables(page)}


async def run(parts: list[str], brand: str, delay: float) -> dict[str, Any]:
    result: dict[str, Any] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="de-DE", timezone_id="Europe/Berlin")
        page = await context.new_page()
        try:
            for idx, part in enumerate(parts):
                result[part] = await fetch_part(page, brand, part, first=(idx == 0))
                if idx < len(parts) - 1:
                    await asyncio.sleep(delay)
        finally:
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
    output = Path(args.output)

    # Never leave stale data behind. A failed run must commit an empty JSON object.
    output.write_text("{}\n", encoding="utf-8")
    try:
        parts = read_parts(Path(args.input))
        if not parts:
            raise RuntimeError("Input contains no part numbers")
        data = asyncio.run(run(parts, args.brand, args.delay))
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        output.write_text("{}\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
