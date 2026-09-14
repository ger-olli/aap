#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

BASE_URL = "https://www.amayama.com/en/part/{brand}/{part_number}"
PRICE_RE = re.compile(r"(?:^|\s)(\d[\d.,]*)\s*(?:EUR|€)?(?:\s|$)")
DEBUG_DIR = Path("debug")


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


async def save_debug(page: Page, label: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        body = await page.locator("body").inner_text()
        (DEBUG_DIR / f"{label}.txt").write_text(body, encoding="utf-8")
    except Exception:
        pass
    try:
        controls = await page.locator("a,button,input,select,[role=button],[role=combobox]").evaluate_all(
            "els => els.map((e,i)=>({i,tag:e.tagName,text:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim(),href:e.href||null,name:e.name||null,id:e.id||null,class:e.className||null})).filter(x=>x.text)"
        )
        (DEBUG_DIR / f"{label}-controls.json").write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


async def accept_cookies(page: Page) -> None:
    for label in ("Reject", "Accept", "Reject all", "Accept all"):
        loc = page.get_by_role("button", name=label, exact=True)
        if await loc.count():
            try:
                await loc.first.click(timeout=1500)
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
                try:
                    if value is not None:
                        await select.select_option(value=value)
                    else:
                        await select.select_option(label=text)
                    await page.wait_for_timeout(1200)
                    return True
                except Exception:
                    pass
    return False


async def country_is_germany(page: Page) -> bool:
    body = await page.locator("body").inner_text()
    return bool(re.search(r"Shipping to\s+Germany", body, re.I))


async def currency_is_eur(page: Page) -> bool:
    body = await page.locator("body").inner_text()
    return bool(re.search(r"Price,\s*EUR", body, re.I))


async def click_visible_text(page: Page, text: str) -> bool:
    for locator in (
        page.get_by_text(text, exact=True),
        page.get_by_role("button", name=text, exact=True),
        page.get_by_role("option", name=text, exact=True),
        page.get_by_role("link", name=text, exact=True),
    ):
        for i in range(await locator.count()):
            item = locator.nth(i)
            try:
                if await item.is_visible():
                    await item.click(timeout=3000)
                    await page.wait_for_timeout(1000)
                    return True
            except Exception:
                pass
    return False


async def try_open_selects_until_germany(page: Page) -> bool:
    # The Amayama page exposes the destination chooser as a generic "Select"
    # control in some layouts. Try each visible one and look for Germany.
    selectors = [
        page.get_by_text("Select", exact=True),
        page.get_by_role("button", name=re.compile(r"select", re.I)),
        page.get_by_role("link", name=re.compile(r"select", re.I)),
    ]
    for group in selectors:
        count = await group.count()
        for i in range(count):
            item = group.nth(i)
            try:
                if not await item.is_visible():
                    continue
                await item.click(timeout=2500)
                await page.wait_for_timeout(500)
                if await click_visible_text(page, "Germany"):
                    await page.wait_for_timeout(1500)
                    if await country_is_germany(page):
                        return True
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            except Exception:
                continue
    return False


async def set_country_germany(page: Page) -> None:
    if await country_is_germany(page):
        return

    if await select_native_option(page, "Germany") and await country_is_germany(page):
        return

    body = await page.locator("body").inner_text()
    m = re.search(r"Shipping to\s+([^\n\[]+)", body, re.I)
    if m:
        current = m.group(1).strip()
        if await click_visible_text(page, current):
            if await click_visible_text(page, "Germany"):
                await page.wait_for_timeout(1500)
                if await country_is_germany(page):
                    return

    if await try_open_selects_until_germany(page):
        return

    # Last resort: searchable country popup/input.
    inputs = page.locator("input")
    for i in range(await inputs.count()):
        inp = inputs.nth(i)
        try:
            if not await inp.is_visible():
                continue
            placeholder = (await inp.get_attribute("placeholder") or "").lower()
            aria = (await inp.get_attribute("aria-label") or "").lower()
            if any(x in placeholder + " " + aria for x in ("country", "shipping", "destination", "search")):
                await inp.fill("Germany")
                await page.wait_for_timeout(500)
                if await click_visible_text(page, "Germany"):
                    await page.wait_for_timeout(1500)
                    if await country_is_germany(page):
                        return
        except Exception:
            pass

    await save_debug(page, "country-selection-failed")
    raise RuntimeError("Could not set shipping country to Germany")


async def set_currency_eur(page: Page) -> None:
    if await currency_is_eur(page):
        return
    if await select_native_option(page, "EUR") and await currency_is_eur(page):
        return
    if await click_visible_text(page, "EUR"):
        await page.wait_for_timeout(1200)
        if await currency_is_eur(page):
            return
    await save_debug(page, "currency-selection-failed")
    raise RuntimeError("Could not set currency to EUR")


async def set_germany_eur(page: Page) -> None:
    await set_country_germany(page)
    await set_currency_eur(page)


async def verify_germany_eur(page: Page) -> None:
    if not await country_is_germany(page):
        raise RuntimeError("Shipping destination is not Germany")
    if not await currency_is_eur(page):
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

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, float, str]] = set()
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
        except Exception:
            await save_debug(page, "run-failed")
            raise
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
