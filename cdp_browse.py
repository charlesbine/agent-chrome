#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.10"]
# ///
"""General-purpose focus-safe CDP helper for agent-chrome profiles.

Talks strictly to a profile's local proxy URL (http://127.0.0.1:<proxy_port>),
opens a background tab, waits for load/render, performs the requested read/eval/
screenshot operation, and always closes its own tab.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote

import aiohttp


class CdpTab:
    """Minimal CDP client bound to a single background tab on the local proxy."""

    def __init__(self, session: aiohttp.ClientSession, proxy_base: str) -> None:
        self.session = session
        self.proxy_base = proxy_base.rstrip("/")
        self.target_id: str | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._msg_id = 0

    async def open(self, url: str) -> None:
        encoded = quote(url, safe=":/?&=+%#@!$'()*,;~-")
        async with self.session.get(f"{self.proxy_base}/json/new?{encoded}") as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Failed to open background tab ({resp.status}): {body}")
            info = await resp.json(content_type=None)
        self.target_id = info["id"]
        ws_url = info["webSocketDebuggerUrl"]
        self.ws = await self.session.ws_connect(ws_url, max_msg_size=0, autoping=True)
        await self.call("Page.enable")
        await self.call("Runtime.enable")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        assert self.ws is not None
        self._msg_id += 1
        req_id = self._msg_id
        payload: dict[str, Any] = {"id": req_id, "method": method}
        if params:
            payload["params"] = params
        await self.ws.send_str(json.dumps(payload, separators=(",", ":")))
        async with asyncio.timeout(timeout):
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if isinstance(data, dict) and data.get("id") == req_id:
                        if "error" in data:
                            raise RuntimeError(f"CDP {method} error: {data['error']}")
                        return data.get("result", {})
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise RuntimeError(f"CDP WebSocket closed while waiting for {method}")
        raise RuntimeError(f"CDP {method} received no response")

    async def eval_json(self, expression: str, timeout: float = 25.0) -> Any:
        res = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    async def wait_ready(self, settle_seconds: float, timeout: float = 20.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await self.eval_json("document.readyState", timeout=5.0)
                if state in ("interactive", "complete"):
                    if state == "complete":
                        break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.target_id:
            try:
                async with self.session.get(
                    f"{self.proxy_base}/json/close/{self.target_id}"
                ):
                    pass
            except aiohttp.ClientError:
                pass


async def check_proxy(session: aiohttp.ClientSession, proxy_base: str) -> None:
    try:
        async with session.get(f"{proxy_base}/json/version") as resp:
            if resp.status != 200:
                raise RuntimeError(f"Proxy at {proxy_base} returned HTTP {resp.status}")
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"Cannot reach proxy at {proxy_base}: {exc}") from exc


async def run_command(args: argparse.Namespace) -> int:
    proxy_base = args.proxy.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=max(45.0, getattr(args, "wait", 0.0) + 35.0), connect=5)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        await check_proxy(session, proxy_base)

        if args.cmd == "tabs":
            async with session.get(f"{proxy_base}/json/list") as resp:
                targets = await resp.json(content_type=None)
            pages = [t for t in targets if t.get("type") == "page"]
            if args.json:
                print(json.dumps(pages, indent=2))
            else:
                if not pages:
                    print("(no open tabs)")
                for t in pages:
                    print(f"{t.get('id', '')}  {t.get('title', '').strip() or '(untitled)'}")
                    print(f"  {t.get('url', '')}")
            return 0

        if args.cmd == "close-tab":
            async with session.get(f"{proxy_base}/json/close/{args.target_id}") as resp:
                body = (await resp.text()).strip()
                if resp.status != 200:
                    print(f"Failed ({resp.status}): {body}", file=sys.stderr)
                    return 1
                print(body or f"Closed {args.target_id}")
            return 0

        tab = CdpTab(session, proxy_base)
        try:
            await tab.open(args.url)
            if args.cmd == "screenshot" and (args.width or args.height):
                await tab.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": args.width or 1440,
                        "height": args.height or 900,
                        "deviceScaleFactor": 2,
                        "mobile": False,
                    },
                )
            await tab.wait_ready(settle_seconds=args.wait)

            if args.cmd == "read":
                for _ in range(max(0, args.scrolls)):
                    await tab.eval_json("window.scrollBy(0, Math.floor(window.innerHeight * 1.25))")
                    await asyncio.sleep(0.9)

                sel_json = json.dumps(args.selector)
                data = await tab.eval_json(
                    f"""(() => {{
                      const sel = {sel_json};
                      const root = sel ? document.querySelector(sel) : (document.body || document.documentElement);
                      const text = root ? (root.innerText || '') : '';
                      return {{
                        url: location.href,
                        title: document.title || '',
                        foundSelector: !sel || !!root,
                        text,
                      }};
                    }})()"""
                )
                text = (data.get("text") or "").strip()
                if args.max_chars and len(text) > args.max_chars:
                    text = text[: args.max_chars] + "\n...[truncated]"
                    data["truncated"] = True
                data["text"] = text
                if args.json:
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                else:
                    print(f"Title: {data.get('title', '')}")
                    print(f"URL:   {data.get('url', '')}")
                    if not data.get("foundSelector", True):
                        print(f"(selector not found: {args.selector})", file=sys.stderr)
                    print()
                    print(text)
                return 0

            if args.cmd == "links":
                sel_json = json.dumps(args.selector)
                raw = await tab.eval_json(
                    f"""(() => {{
                      const sel = {sel_json};
                      const root = sel ? document.querySelector(sel) : document;
                      if (!root) return {{ url: location.href, title: document.title || '', links: [] }};
                      const seen = new Set();
                      const links = [];
                      for (const a of root.querySelectorAll('a[href]')) {{
                        const href = a.href || '';
                        if (!href || href.startsWith('javascript:')) continue;
                        const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\\s+/g, ' ').trim();
                        const key = href + '\\0' + text;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        links.push({{ text, href }});
                      }}
                      return {{ url: location.href, title: document.title || '', links }};
                    }})()"""
                )
                links = raw.get("links") or []
                if args.filter:
                    pat = re.compile(args.filter, re.IGNORECASE)
                    links = [
                        item
                        for item in links
                        if pat.search(item.get("href", "")) or pat.search(item.get("text", ""))
                    ]
                if args.limit:
                    links = links[: args.limit]
                raw["links"] = links
                if args.json:
                    print(json.dumps(raw, indent=2, ensure_ascii=False))
                else:
                    print(f"Title: {raw.get('title', '')}")
                    print(f"URL:   {raw.get('url', '')}\n")
                    for idx, item in enumerate(links, 1):
                        label = item["text"] or "(no text)"
                        print(f"{idx}. {label}\n   {item['href']}")
                return 0

            if args.cmd == "eval":
                value = await tab.eval_json(args.js)
                if args.json or not isinstance(value, str):
                    print(json.dumps(value, indent=2, ensure_ascii=False))
                else:
                    print(value)
                return 0

            if args.cmd == "screenshot":
                shot_params: dict[str, Any] = {"format": "png"}
                if args.full_page:
                    shot_params["captureBeyondViewport"] = True
                res = await tab.call("Page.captureScreenshot", shot_params, timeout=30.0)
                png_bytes = base64.b64decode(res["data"])
                out_path = Path(args.out).expanduser()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(png_bytes)
                print(str(out_path.resolve()))
                return 0

            raise ValueError(f"Unknown command: {args.cmd}")
        finally:
            await tab.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Focus-safe CDP browser helper.")
    parser.add_argument("--proxy", required=True, help="Local CDP proxy base URL (e.g. http://127.0.0.1:9225).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tabs = sub.add_parser("tabs", help="List open page tabs.")
    p_tabs.add_argument("--json", action="store_true")

    p_close = sub.add_parser("close-tab", help="Close an open tab by target ID.")
    p_close.add_argument("target_id")

    p_read = sub.add_parser("read", help="Open a URL in a background tab, extract text, and close it.")
    p_read.add_argument("url")
    p_read.add_argument("--wait", type=float, default=2.0, help="Extra settle seconds after load (default: 2.0).")
    p_read.add_argument("--selector", default="", help="Optional CSS selector to read.")
    p_read.add_argument("--scrolls", type=int, default=0, help="Number of incremental scrolls before reading.")
    p_read.add_argument("--max-chars", type=int, default=20000, help="Max text chars to return (0 = unlimited).")
    p_read.add_argument("--json", action="store_true")

    p_links = sub.add_parser("links", help="Extract links from a page in a background tab.")
    p_links.add_argument("url")
    p_links.add_argument("--wait", type=float, default=2.0)
    p_links.add_argument("--selector", default="")
    p_links.add_argument("--filter", default="", help="Case-insensitive regex filter on link text or URL.")
    p_links.add_argument("--limit", type=int, default=100)
    p_links.add_argument("--json", action="store_true")

    p_eval = sub.add_parser("eval", help="Navigate to a URL and evaluate a JS expression.")
    p_eval.add_argument("url")
    p_eval.add_argument("js")
    p_eval.add_argument("--wait", type=float, default=2.0)
    p_eval.add_argument("--json", action="store_true")

    p_shot = sub.add_parser("screenshot", help="Capture a PNG screenshot of a URL in a background tab.")
    p_shot.add_argument("url")
    p_shot.add_argument("--out", required=True, help="Output PNG path.")
    p_shot.add_argument("--wait", type=float, default=2.0)
    p_shot.add_argument("--full-page", action="store_true")
    p_shot.add_argument("--width", type=int, default=0)
    p_shot.add_argument("--height", type=int, default=0)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_command(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
