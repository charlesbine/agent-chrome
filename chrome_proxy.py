#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.10"]
# ///
"""Focus-safe, optional no-typing local Chrome DevTools Protocol proxy.

Listens on 127.0.0.1:<AGENT_CHROME_PROXY_PORT> (default 9225) and forwards
to Chrome on 127.0.0.1:<AGENT_CHROME_PORT> (default 9224).

Safety guarantees:
- Prevents Chrome from stealing OS window focus by rewriting Target.createTarget
  (background=true), swallowing Page.bringToFront and Target.activateTarget,
  implementing /json/new in the background, and no-oping /json/activate/<id>.
- In default no-typing mode, allows normal mouse clicks and scrolling so agents
  can navigate UIs ("Show more", tabs, feeds) while blocking keyboard, text,
  and IME input methods (no typing or pasting). Pass --allow-typing for
  write-capable profiles.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from aiohttp import web

LISTEN_HOST = "127.0.0.1"
UPSTREAM_HOST = "127.0.0.1"
DEFAULT_UPSTREAM_PORT = 9224
DEFAULT_PROXY_PORT = 9225

SWALLOWED_METHODS: frozenset[str] = frozenset(
    {"Page.bringToFront", "Target.activateTarget"}
)

BLOCKED_TYPING_METHODS: frozenset[str] = frozenset(
    {
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.imeSetComposition",
    }
)

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "content-encoding",
    }
)

LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

logger = logging.getLogger("agent_chrome_proxy")


class CdpAction(Enum):
    """Disposition of a client-to-Chrome CDP text frame."""

    FORWARD = "forward"
    REWRITE = "rewrite"
    SWALLOW = "swallow"
    BLOCK = "block"


@dataclass(frozen=True)
class CdpDecision:
    """Result of inspecting a single client-to-Chrome CDP text frame."""

    action: CdpAction
    upstream_text: str | None = None
    synthetic_reply: str | None = None


@dataclass(frozen=True)
class ProxyConfig:
    """Runtime address and guard configuration for the proxy."""

    upstream_port: int = DEFAULT_UPSTREAM_PORT
    proxy_port: int = DEFAULT_PROXY_PORT
    listen_host: str = LISTEN_HOST
    upstream_host: str = UPSTREAM_HOST
    block_typing: bool = True


@dataclass
class ProxyStats:
    """Cumulative proxy counters logged at close/shutdown."""

    connections_opened: int = 0
    rewritten: int = 0
    swallowed: int = 0
    blocked: int = 0


def read_port_env(name: str, default: int) -> int:
    """Read a TCP port from an environment variable with validation."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        port = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid port in {name}: {raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"Port out of range in {name}: {port}")
    return port


def process_client_cdp_message(raw_text: str, *, block_typing: bool = True) -> CdpDecision:
    """Inspect a client-to-Chrome CDP text frame and decide how to handle it."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return CdpDecision(action=CdpAction.FORWARD, upstream_text=raw_text)

    if not isinstance(data, dict):
        return CdpDecision(action=CdpAction.FORWARD, upstream_text=raw_text)

    method = data.get("method")
    if block_typing and method in BLOCKED_TYPING_METHODS:
        synthetic_reply: str | None = None
        if "id" in data:
            reply: dict[str, Any] = {
                "id": data["id"],
                "error": {
                    "code": -32000,
                    "message": f"Blocked by no-typing proxy: {method}",
                },
            }
            if "sessionId" in data and data["sessionId"] is not None:
                reply["sessionId"] = data["sessionId"]
            synthetic_reply = json.dumps(reply, separators=(",", ":"))
        return CdpDecision(
            action=CdpAction.BLOCK,
            upstream_text=None,
            synthetic_reply=synthetic_reply,
        )

    if method == "Target.createTarget":
        raw_params = data.get("params")
        if isinstance(raw_params, dict) and raw_params.get("background") is True:
            return CdpDecision(action=CdpAction.FORWARD, upstream_text=raw_text)
        updated = dict(data)
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
        params["background"] = True
        updated["params"] = params
        return CdpDecision(
            action=CdpAction.REWRITE,
            upstream_text=json.dumps(updated, separators=(",", ":")),
        )

    if method in SWALLOWED_METHODS:
        synthetic_reply = None
        if "id" in data:
            reply = {"id": data["id"], "result": {}}
            if "sessionId" in data and data["sessionId"] is not None:
                reply["sessionId"] = data["sessionId"]
            synthetic_reply = json.dumps(reply, separators=(",", ":"))
        return CdpDecision(
            action=CdpAction.SWALLOW,
            upstream_text=None,
            synthetic_reply=synthetic_reply,
        )

    return CdpDecision(action=CdpAction.FORWARD, upstream_text=raw_text)


def rewrite_ws_url(
    url: str,
    upstream_port: int,
    proxy_port: int,
    proxy_host: str = LISTEN_HOST,
) -> str:
    """Rewrite a ws:// or wss:// debugger URL pointing at upstream_port."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return url
    if (
        parsed.scheme in ("ws", "wss")
        and parsed.hostname in LOCAL_HOSTS
        and port == upstream_port
    ):
        return urlunsplit(
            (
                parsed.scheme,
                f"{proxy_host}:{proxy_port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    return url


def rewrite_devtools_frontend_url(
    url: str,
    upstream_port: int,
    proxy_port: int,
    proxy_host: str = LISTEN_HOST,
) -> str:
    """Rewrite upstream host:port references inside a devtoolsFrontendUrl."""
    host_pattern = r"(?:127\.0\.0\.1|localhost|\[::1\])"
    rewritten = re.sub(
        rf"(\b(?:ws|wss)=){host_pattern}:{upstream_port}(?=/|&|$)",
        rf"\g<1>{proxy_host}:{proxy_port}",
        url,
    )
    rewritten = re.sub(
        rf"(\bhttps?://){host_pattern}:{upstream_port}(?=/|$)",
        rf"\g<1>{proxy_host}:{proxy_port}",
        rewritten,
    )
    return rewritten


def rewrite_discovery_payload(
    payload: Any,
    upstream_port: int,
    proxy_port: int,
    proxy_host: str = LISTEN_HOST,
) -> Any:
    """Recursively rewrite webSocketDebuggerUrl and devtoolsFrontendUrl fields."""
    if isinstance(payload, list):
        return [
            rewrite_discovery_payload(item, upstream_port, proxy_port, proxy_host)
            for item in payload
        ]
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "webSocketDebuggerUrl" and isinstance(value, str):
                result[key] = rewrite_ws_url(
                    value, upstream_port, proxy_port, proxy_host
                )
            elif key == "devtoolsFrontendUrl" and isinstance(value, str):
                result[key] = rewrite_devtools_frontend_url(
                    value, upstream_port, proxy_port, proxy_host
                )
            elif isinstance(value, (dict, list)):
                result[key] = rewrite_discovery_payload(
                    value, upstream_port, proxy_port, proxy_host
                )
            else:
                result[key] = value
        return result
    return payload


def extract_new_target_url(raw_path: str) -> str:
    """Extract the target URL from a /json/new[?<url>] request raw_path."""
    if "?" not in raw_path:
        return "about:blank"
    query = raw_path.split("?", 1)[1]
    return query if query else "about:blank"


def build_fallback_target_info(
    target_id: str,
    url: str,
    proxy_port: int,
    proxy_host: str = LISTEN_HOST,
) -> dict[str, str]:
    """Construct a discovery target dict if /json/list did not yet include it."""
    return {
        "description": "",
        "devtoolsFrontendUrl": (
            f"/devtools/inspector.html?ws={proxy_host}:{proxy_port}"
            f"/devtools/page/{target_id}"
        ),
        "id": target_id,
        "title": "",
        "type": "page",
        "url": url,
        "webSocketDebuggerUrl": (
            f"ws://{proxy_host}:{proxy_port}/devtools/page/{target_id}"
        ),
    }


def filter_forward_headers(headers: Any) -> dict[str, str]:
    """Drop hop-by-hop and host/length headers before forwarding HTTP traffic."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def create_app(config: ProxyConfig) -> web.Application:
    """Build the aiohttp web application for the CDP proxy."""
    import aiohttp
    from aiohttp import web

    stats = ProxyStats()
    active_websockets: set[web.WebSocketResponse] = set()
    client_session_key: web.AppKey[aiohttp.ClientSession] = web.AppKey(
        "client_session", aiohttp.ClientSession
    )

    def upstream_http_url(raw_path: str) -> str:
        return f"http://{config.upstream_host}:{config.upstream_port}{raw_path}"

    def upstream_ws_url(raw_path: str) -> str:
        return f"ws://{config.upstream_host}:{config.upstream_port}{raw_path}"

    async def handle_json_discovery(request: web.Request) -> web.StreamResponse:
        session = request.app[client_session_key]
        body = await request.read()
        try:
            async with session.request(
                request.method,
                upstream_http_url(request.raw_path),
                headers=filter_forward_headers(request.headers),
                data=body if body else None,
                allow_redirects=False,
            ) as upstream_resp:
                raw_body = await upstream_resp.read()
                if upstream_resp.status == 200:
                    try:
                        parsed = json.loads(raw_body)
                    except json.JSONDecodeError:
                        return web.Response(
                            status=upstream_resp.status,
                            body=raw_body,
                            headers=filter_forward_headers(upstream_resp.headers),
                        )
                    rewritten = rewrite_discovery_payload(
                        parsed,
                        upstream_port=config.upstream_port,
                        proxy_port=config.proxy_port,
                        proxy_host=config.listen_host,
                    )
                    return web.Response(
                        status=200,
                        text=json.dumps(rewritten, indent=2),
                        content_type="application/json",
                    )
                return web.Response(
                    status=upstream_resp.status,
                    body=raw_body,
                    headers=filter_forward_headers(upstream_resp.headers),
                )
        except aiohttp.ClientError as exc:
            logger.warning("upstream HTTP error (%s)", type(exc).__name__)
            return web.Response(status=502, text="Upstream Chrome unavailable")

    async def handle_json_new(request: web.Request) -> web.StreamResponse:
        session = request.app[client_session_key]
        target_url = extract_new_target_url(request.raw_path)

        try:
            async with session.get(upstream_http_url("/json/version")) as ver_resp:
                if ver_resp.status != 200:
                    return web.Response(
                        status=502,
                        text="Failed to query upstream Chrome /json/version",
                    )
                version_data = await ver_resp.json(content_type=None)
        except (aiohttp.ClientError, json.JSONDecodeError) as exc:
            logger.warning("upstream /json/version error (%s)", type(exc).__name__)
            return web.Response(status=502, text="Upstream Chrome unavailable")

        browser_ws_url = version_data.get("webSocketDebuggerUrl")
        if not isinstance(browser_ws_url, str) or not browser_ws_url:
            return web.Response(
                status=502,
                text="Upstream Chrome did not expose browser WebSocket URL",
            )

        target_id: str | None = None
        try:
            async with session.ws_connect(
                browser_ws_url,
                max_msg_size=0,
                autoping=True,
                heartbeat=None,
            ) as browser_ws:
                cmd = {
                    "id": 1,
                    "method": "Target.createTarget",
                    "params": {"url": target_url, "background": True},
                }
                await browser_ws.send_str(json.dumps(cmd, separators=(",", ":")))
                async with asyncio.timeout(15.0):
                    async for msg in browser_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            reply = json.loads(msg.data)
                            if isinstance(reply, dict) and reply.get("id") == 1:
                                if "error" in reply:
                                    return web.json_response(
                                        {"error": reply["error"]}, status=500
                                    )
                                result_obj = reply.get("result")
                                if isinstance(result_obj, dict) and isinstance(
                                    result_obj.get("targetId"), str
                                ):
                                    target_id = result_obj["targetId"]
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except TimeoutError:
            return web.Response(status=504, text="Timed out creating background target")
        except aiohttp.ClientError as exc:
            logger.warning("upstream browser WS error (%s)", type(exc).__name__)
            return web.Response(status=502, text="Upstream Chrome unavailable")

        if not target_id:
            return web.Response(status=502, text="Upstream Chrome returned no targetId")

        stats.rewritten += 1

        try:
            async with session.get(upstream_http_url("/json/list")) as list_resp:
                if list_resp.status == 200:
                    targets = await list_resp.json(content_type=None)
                    if isinstance(targets, list):
                        for entry in targets:
                            if isinstance(entry, dict) and entry.get("id") == target_id:
                                rewritten = rewrite_discovery_payload(
                                    entry,
                                    upstream_port=config.upstream_port,
                                    proxy_port=config.proxy_port,
                                    proxy_host=config.listen_host,
                                )
                                return web.Response(
                                    status=200,
                                    text=json.dumps(rewritten, indent=2),
                                    content_type="application/json",
                                )
        except (aiohttp.ClientError, json.JSONDecodeError):
            pass

        fallback = build_fallback_target_info(
            target_id=target_id,
            url=target_url,
            proxy_port=config.proxy_port,
            proxy_host=config.listen_host,
        )
        return web.Response(
            status=200,
            text=json.dumps(fallback, indent=2),
            content_type="application/json",
        )

    async def handle_json_activate(request: web.Request) -> web.StreamResponse:
        del request
        stats.swallowed += 1
        return web.Response(
            status=200,
            text="Target activated",
            content_type="text/plain",
        )

    async def handle_http_passthrough(request: web.Request) -> web.StreamResponse:
        session = request.app[client_session_key]
        body = await request.read()
        try:
            async with session.request(
                request.method,
                upstream_http_url(request.raw_path),
                headers=filter_forward_headers(request.headers),
                data=body if body else None,
                allow_redirects=False,
            ) as upstream_resp:
                raw_body = await upstream_resp.read()
                return web.Response(
                    status=upstream_resp.status,
                    body=raw_body,
                    headers=filter_forward_headers(upstream_resp.headers),
                )
        except aiohttp.ClientError as exc:
            logger.warning("upstream HTTP passthrough error (%s)", type(exc).__name__)
            return web.Response(status=502, text="Upstream Chrome unavailable")

    async def handle_websocket(request: web.Request) -> web.StreamResponse:
        session = request.app[client_session_key]
        ws_url = upstream_ws_url(request.raw_path)

        try:
            upstream_ws = await session.ws_connect(
                ws_url,
                max_msg_size=0,
                autoping=True,
                heartbeat=None,
            )
        except aiohttp.WSServerHandshakeError as exc:
            return web.Response(status=exc.status, text=exc.message)
        except aiohttp.ClientError as exc:
            logger.warning("upstream WS connect error (%s)", type(exc).__name__)
            return web.Response(status=502, text="Upstream Chrome unavailable")

        client_ws = web.WebSocketResponse(
            max_msg_size=0,
            autoping=True,
            heartbeat=None,
        )
        await client_ws.prepare(request)
        active_websockets.add(client_ws)

        stats.connections_opened += 1
        conn_id = stats.connections_opened
        conn_rewritten = 0
        conn_swallowed = 0
        conn_blocked = 0
        client_write_lock = asyncio.Lock()

        logger.info("ws #%d opened path=%s", conn_id, request.path)

        async def forward_client_to_upstream() -> None:
            nonlocal conn_rewritten, conn_swallowed, conn_blocked
            async for msg in client_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    decision = process_client_cdp_message(
                        msg.data, block_typing=config.block_typing
                    )
                    if decision.action is CdpAction.FORWARD:
                        assert decision.upstream_text is not None
                        await upstream_ws.send_str(decision.upstream_text)
                    elif decision.action is CdpAction.REWRITE:
                        conn_rewritten += 1
                        stats.rewritten += 1
                        assert decision.upstream_text is not None
                        await upstream_ws.send_str(decision.upstream_text)
                    elif decision.action is CdpAction.SWALLOW:
                        conn_swallowed += 1
                        stats.swallowed += 1
                        if decision.synthetic_reply is not None:
                            async with client_write_lock:
                                if not client_ws.closed:
                                    await client_ws.send_str(decision.synthetic_reply)
                    elif decision.action is CdpAction.BLOCK:
                        conn_blocked += 1
                        stats.blocked += 1
                        logger.warning("ws #%d blocked write/input CDP call", conn_id)
                        if decision.synthetic_reply is not None:
                            async with client_write_lock:
                                if not client_ws.closed:
                                    await client_ws.send_str(decision.synthetic_reply)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await upstream_ws.send_bytes(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

        async def forward_upstream_to_client() -> None:
            async for msg in upstream_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    async with client_write_lock:
                        if client_ws.closed:
                            break
                        await client_ws.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    async with client_write_lock:
                        if client_ws.closed:
                            break
                        await client_ws.send_bytes(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

        c2u = asyncio.create_task(forward_client_to_upstream())
        u2c = asyncio.create_task(forward_upstream_to_client())
        try:
            _, pending = await asyncio.wait(
                {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in (c2u, u2c):
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc is not None and not isinstance(
                        exc, (ConnectionResetError, aiohttp.ClientError)
                    ):
                        logger.warning(
                            "ws #%d forwarding error (%s)", conn_id, type(exc).__name__
                        )
        finally:
            active_websockets.discard(client_ws)
            if not upstream_ws.closed:
                await upstream_ws.close()
            if not client_ws.closed:
                await client_ws.close()
            logger.info(
                "ws #%d closed path=%s rewritten=%d swallowed=%d blocked=%d "
                "(total rewritten=%d swallowed=%d blocked=%d)",
                conn_id,
                request.path,
                conn_rewritten,
                conn_swallowed,
                conn_blocked,
                stats.rewritten,
                stats.swallowed,
                stats.blocked,
            )

        return client_ws

    async def dispatch(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await handle_websocket(request)

        path = request.path
        if path in ("/json/new", "/json/new/"):
            return await handle_json_new(request)
        if path == "/json/activate" or path.startswith("/json/activate/"):
            return await handle_json_activate(request)
        if path in (
            "/json",
            "/json/",
            "/json/list",
            "/json/list/",
            "/json/version",
            "/json/version/",
        ):
            return await handle_json_discovery(request)
        return await handle_http_passthrough(request)

    async def on_startup(app: web.Application) -> None:
        app[client_session_key] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=5)
        )
        mode = "no-typing guard enabled" if config.block_typing else "typing allowed"
        logger.info(
            "listening on %s:%d -> upstream %s:%d (%s)",
            config.listen_host,
            config.proxy_port,
            config.upstream_host,
            config.upstream_port,
            mode,
        )

    async def on_shutdown(app: web.Application) -> None:
        del app
        for ws in list(active_websockets):
            await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
        logger.info(
            "shutdown connections=%d total_rewritten=%d total_swallowed=%d total_blocked=%d",
            stats.connections_opened,
            stats.rewritten,
            stats.swallowed,
            stats.blocked,
        )

    async def on_cleanup(app: web.Application) -> None:
        await app[client_session_key].close()

    app = web.Application(client_max_size=0)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", dispatch)
    return app


def parse_args(argv: list[str] | None = None) -> ProxyConfig:
    """Parse CLI flags with environment variable defaults."""
    parser = argparse.ArgumentParser(
        description="Focus-safe, optional no-typing local CDP proxy for agent Chrome profiles."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=read_port_env("AGENT_CHROME_PROXY_PORT", DEFAULT_PROXY_PORT),
        help="Local proxy port on 127.0.0.1 (env: AGENT_CHROME_PROXY_PORT, default: 9225).",
    )
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=read_port_env("AGENT_CHROME_PORT", DEFAULT_UPSTREAM_PORT),
        help="Upstream Chrome CDP port on 127.0.0.1 (env: AGENT_CHROME_PORT, default: 9224).",
    )
    parser.add_argument(
        "--allow-typing",
        action="store_true",
        help="Allow CDP keyboard/text input (for write-capable profiles).",
    )
    args = parser.parse_args(argv)
    return ProxyConfig(
        upstream_port=args.upstream_port,
        proxy_port=args.port,
        block_typing=not args.allow_typing,
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point for running the CDP proxy."""
    from aiohttp import web

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [agent-chrome-proxy] %(message)s",
    )
    config = parse_args(argv)
    app = create_app(config)
    web.run_app(
        app,
        host=config.listen_host,
        port=config.proxy_port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
