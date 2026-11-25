"""Unit tests for chrome_proxy.py CDP guards and URL rewriting."""

from __future__ import annotations

import json
import unittest

import chrome_proxy


class ChromeProxyTests(unittest.TestCase):
    def test_swallows_focus_stealing_methods(self) -> None:
        for method in ("Page.bringToFront", "Target.activateTarget"):
            msg = json.dumps({"id": 7, "method": method, "sessionId": "s1"})
            decision = chrome_proxy.process_client_cdp_message(msg, block_typing=True)
            self.assertEqual(decision.action, chrome_proxy.CdpAction.SWALLOW)
            self.assertIsNone(decision.upstream_text)
            reply = json.loads(decision.synthetic_reply or "{}")
            self.assertEqual(reply, {"id": 7, "result": {}, "sessionId": "s1"})

    def test_rewrites_create_target_to_background(self) -> None:
        msg = json.dumps({"id": 3, "method": "Target.createTarget", "params": {"url": "https://example.com"}})
        decision = chrome_proxy.process_client_cdp_message(msg, block_typing=True)
        self.assertEqual(decision.action, chrome_proxy.CdpAction.REWRITE)
        rewritten = json.loads(decision.upstream_text or "{}")
        self.assertTrue(rewritten["params"]["background"])

    def test_blocks_typing_when_enabled_but_allows_mouse(self) -> None:
        for method in ("Input.dispatchKeyEvent", "Input.insertText", "Input.imeSetComposition"):
            msg = json.dumps({"id": 11, "method": method, "params": {"text": "hi"}})
            blocked = chrome_proxy.process_client_cdp_message(msg, block_typing=True)
            self.assertEqual(blocked.action, chrome_proxy.CdpAction.BLOCK)
            self.assertIsNone(blocked.upstream_text)
            err = json.loads(blocked.synthetic_reply or "{}")
            self.assertEqual(err["id"], 11)
            self.assertIn("Blocked by no-typing proxy", err["error"]["message"])

            allowed = chrome_proxy.process_client_cdp_message(msg, block_typing=False)
            self.assertEqual(allowed.action, chrome_proxy.CdpAction.FORWARD)

        mouse = json.dumps({"id": 12, "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed"}})
        decision = chrome_proxy.process_client_cdp_message(mouse, block_typing=True)
        self.assertEqual(decision.action, chrome_proxy.CdpAction.FORWARD)

    def test_rewrites_discovery_urls(self) -> None:
        payload = [
            {
                "id": "T1",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9224/devtools/page/T1",
                "devtoolsFrontendUrl": "/devtools/inspector.html?ws=127.0.0.1:9224/devtools/page/T1",
            }
        ]
        out = chrome_proxy.rewrite_discovery_payload(payload, upstream_port=9224, proxy_port=9225)
        self.assertEqual(out[0]["webSocketDebuggerUrl"], "ws://127.0.0.1:9225/devtools/page/T1")
        self.assertIn("ws=127.0.0.1:9225/devtools/page/T1", out[0]["devtoolsFrontendUrl"])


if __name__ == "__main__":
    unittest.main()
