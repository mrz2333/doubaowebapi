"""Diagnostic script to discover all Doubao skill buttons and their data-skill-id values.

Usage:
    python -m doubaowebapi.dropdown_debug
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doubaowebapi.browser_client import CHAT_URL, BrowserClient


async def main():
    client = BrowserClient(headless=False)
    try:
        print("Starting browser...")
        await client.start()
        print("Checking session...")

        page = client._page
        if not page:
            pages = client._context.pages if client._context else []
            if pages:
                page = pages[-1]
            else:
                page = await client._context.new_page()
                await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(8)

        if not client.is_ready:
            print("NOT READY - opening chat page...")
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)

        print(f"Page URL: {page.url}")

        # Scan ALL skill buttons
        print("\n=== Scanning all skill_bar_button elements ===")
        skills = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('[data-skill-id]').forEach(el => {
                const rect = el.getBoundingClientRect();
                results.push({
                    skill_id: el.getAttribute('data-skill-id'),
                    text: (el.textContent || '').trim().slice(0, 100),
                    tag: el.tagName,
                    className: (el.className?.toString?.() || el.className || '').slice(0, 150),
                    visible: rect.width > 0 && rect.height > 0,
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                    ariaLabel: el.getAttribute('aria-label') || '',
                    title: el.getAttribute('title') || '',
                });
            });
            return results;
        }""")

        print(json.dumps(skills, indent=2, ensure_ascii=False))

        # Also scan the sidebar/action bar for all clickable items
        print("\n=== Scanning sidebar/action bar items ===")
        bar_items = await page.evaluate("""() => {
            const results = [];
            const bars = document.querySelectorAll('[class*="bar"], [class*="action"], [class*="skill"], [class*="sidebar"], [class*="toolbar"]');
            bars.forEach(bar => {
                const buttons = bar.querySelectorAll('button, [role="button"], div[class*="item"], div[class*="button"]');
                buttons.forEach(btn => {
                    const rect = btn.getBoundingClientRect();
                    const text = (btn.textContent || '').trim().slice(0, 80);
                    if (text && rect.width > 0 && rect.height > 0) {
                        results.push({
                            text: text,
                            dataSkillId: btn.getAttribute('data-skill-id') || '',
                            className: (btn.className?.toString?.() || btn.className || '').slice(0, 150),
                            rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                        });
                    }
                });
            });
            return results;
        }""")

        print(json.dumps(bar_items, indent=2, ensure_ascii=False))

        print("\n=== Done! Keeping browser open. Press Ctrl+C to exit ===")
        await asyncio.sleep(300)

    finally:
        pass


if __name__ == "__main__":
    asyncio.run(main())
