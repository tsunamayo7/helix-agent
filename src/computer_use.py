"""Computer Use handler: unified interface for helix-pilot, agent-browser, and Playwright."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .agent_browser_session import AgentBrowserSession, is_available as _agent_browser_available
from .vision import VisionAnalyzer

HELIX_PILOT_URL = "http://localhost:8765"

try:
    from playwright.async_api import async_playwright, Browser, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


async def _helix_pilot_available(url: str = HELIX_PILOT_URL) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
            return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


async def _helix_pilot_call(method: str, params: dict | None = None, url: str = HELIX_PILOT_URL) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": f"tools/{method}",
        "params": params or {},
        "id": 1,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return {"error": data["error"].get("message", str(data["error"]))}
        return data.get("result", {})


class PlaywrightSession:
    """Manages a Playwright browser session (lazy init)."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def _ensure(self) -> Any:
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright is not installed. Install with: pip install playwright && playwright install chromium")
        if self._page is None:
            self._pw = await async_playwright().__aenter__()
            self._browser = await self._pw.chromium.launch(headless=True)
            self._page = await self._browser.new_page()
        return self._page

    @property
    def page(self) -> Any:
        return self._page

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._page = None
        if self._pw:
            await self._pw.__aexit__(None, None, None)
            self._pw = None

    async def navigate(self, url: str) -> str:
        page = await self._ensure()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"Navigated to {page.url}"

    async def click(self, selector: str) -> str:
        page = await self._ensure()
        await page.click(selector, timeout=10000)
        return f"Clicked: {selector}"

    async def type_text(self, selector: str, text: str) -> str:
        page = await self._ensure()
        await page.fill(selector, text, timeout=10000)
        return f"Typed into: {selector}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._ensure()
        delta = amount if direction == "down" else -amount
        await page.mouse.wheel(0, delta)
        return f"Scrolled {direction} by {amount}px"

    async def screenshot(self) -> str:
        """Take screenshot and return base64-encoded PNG."""
        page = await self._ensure()
        raw = await page.screenshot(type="png")
        return base64.b64encode(raw).decode("ascii")

    async def read_page(self) -> str:
        page = await self._ensure()
        text = await page.evaluate("() => document.body.innerText")
        if len(text) > 8000:
            text = text[:8000] + "\n... (truncated)"
        return text


class ComputerUseHandler:
    """Unified Computer Use interface routing to helix-pilot or Playwright."""

    def __init__(
        self,
        helix_pilot_url: str = HELIX_PILOT_URL,
        vision_analyzer: VisionAnalyzer | None = None,
        prefer_agent_browser: bool = True,
    ):
        self.helix_pilot_url = helix_pilot_url
        self.vision = vision_analyzer or VisionAnalyzer()
        self._pw_session: PlaywrightSession | None = None
        self._ab_session: AgentBrowserSession | None = None
        self._use_pilot: bool | None = None
        self._prefer_agent_browser = prefer_agent_browser

    async def _resolve_backend(self) -> str:
        """Determine which backend to use.

        Priority (when prefer_agent_browser=True, default):
          1. agent-browser (fastest, React-friendly, 82-93% fewer tokens)
          2. helix-pilot (desktop GUI + browser)
          3. playwright (fallback)

        Returns 'agent_browser', 'pilot', 'playwright', or 'none'.
        """
        if self._prefer_agent_browser and _agent_browser_available():
            return "agent_browser"
        if self._use_pilot is None:
            self._use_pilot = await _helix_pilot_available(self.helix_pilot_url)
        if self._use_pilot:
            return "pilot"
        if HAS_PLAYWRIGHT:
            return "playwright"
        return "none"

    def _get_pw_session(self) -> PlaywrightSession:
        if self._pw_session is None:
            self._pw_session = PlaywrightSession()
        return self._pw_session

    def _get_ab_session(self) -> AgentBrowserSession:
        if self._ab_session is None:
            self._ab_session = AgentBrowserSession()
        return self._ab_session

    async def execute(self, params: dict) -> dict:
        """Execute a computer use action.

        Args:
            params: Dict with keys:
                - action: screenshot|click|type|scroll|read_page|navigate
                - target: CSS selector or description (for click/type)
                - value: Text to type or scroll direction
                - url: URL for navigate
                - analyze: bool, if True run Vision on screenshots
                - prompt: Vision prompt for analyze
        """
        action = params.get("action", "")
        target = params.get("target", "")
        value = params.get("value", "")
        url = params.get("url", "")
        analyze = params.get("analyze", False)
        prompt = params.get("prompt", "Describe what you see in this image.")

        backend = await self._resolve_backend()
        if backend == "none":
            return {"error": "No backend available. Install Playwright or start helix-pilot."}

        handler_map = {
            "screenshot": self._do_screenshot,
            "click": self._do_click,
            "type": self._do_type,
            "scroll": self._do_scroll,
            "read_page": self._do_read_page,
            "navigate": self._do_navigate,
        }

        handler = handler_map.get(action)
        if not handler:
            return {"error": f"Unknown action: {action}. Available: {', '.join(handler_map)}"}

        result = await handler(backend=backend, target=target, value=value, url=url)

        if action == "screenshot" and analyze and "image_base64" in result:
            vision_result = await self.vision.analyze(result["image_base64"], prompt=prompt)
            result["analysis"] = vision_result

        return result

    async def _do_screenshot(self, *, backend: str, **kwargs) -> dict:
        if backend == "agent_browser":
            ab = self._get_ab_session()
            b64 = await ab.screenshot()
            return {"backend": "agent-browser", "image_base64": b64}
        if backend == "pilot":
            resp = await _helix_pilot_call("take_screenshot", url=self.helix_pilot_url)
            if "error" in resp:
                return resp
            image_data = resp.get("image", resp.get("screenshot", ""))
            if image_data:
                return {"backend": "helix-pilot", "image_base64": image_data}
            return {"backend": "helix-pilot", "result": str(resp)}

        pw = self._get_pw_session()
        b64 = await pw.screenshot()
        return {"backend": "playwright", "image_base64": b64}

    async def _do_click(self, *, backend: str, target: str, **kwargs) -> dict:
        if not target:
            return {"error": "target is required for click action"}
        if backend == "agent_browser":
            ab = self._get_ab_session()
            msg = await ab.click(target)
            return {"backend": "agent-browser", "result": msg}
        if backend == "pilot":
            resp = await _helix_pilot_call("click_element", {"target": target}, url=self.helix_pilot_url)
            return {"backend": "helix-pilot", **resp}

        pw = self._get_pw_session()
        msg = await pw.click(target)
        return {"backend": "playwright", "result": msg}

    async def _do_type(self, *, backend: str, target: str, value: str, **kwargs) -> dict:
        if not target:
            return {"error": "target is required for type action"}
        if backend == "agent_browser":
            ab = self._get_ab_session()
            # agent-browser's 'fill' clears and types with native keyboard events,
            # bypassing React controlled component rejection.
            msg = await ab.type_text(target, value)
            return {"backend": "agent-browser", "result": msg}
        if backend == "pilot":
            resp = await _helix_pilot_call("type_text", {"target": target, "text": value}, url=self.helix_pilot_url)
            return {"backend": "helix-pilot", **resp}

        pw = self._get_pw_session()
        msg = await pw.type_text(target, value)
        return {"backend": "playwright", "result": msg}

    async def _do_scroll(self, *, backend: str, value: str, **kwargs) -> dict:
        direction = value if value in ("up", "down") else "down"
        if backend == "agent_browser":
            ab = self._get_ab_session()
            msg = await ab.scroll(direction)
            return {"backend": "agent-browser", "result": msg}
        if backend == "pilot":
            resp = await _helix_pilot_call("scroll", {"direction": direction}, url=self.helix_pilot_url)
            return {"backend": "helix-pilot", **resp}

        pw = self._get_pw_session()
        msg = await pw.scroll(direction)
        return {"backend": "playwright", "result": msg}

    async def _do_read_page(self, *, backend: str, **kwargs) -> dict:
        if backend == "agent_browser":
            ab = self._get_ab_session()
            text = await ab.read_page()
            return {"backend": "agent-browser", "text": text}
        if backend == "pilot":
            return {"error": "read_page requires Playwright or agent-browser backend."}

        pw = self._get_pw_session()
        text = await pw.read_page()
        return {"backend": "playwright", "text": text}

    async def _do_navigate(self, *, backend: str, url: str, **kwargs) -> dict:
        if not url:
            return {"error": "url is required for navigate action"}
        if backend == "agent_browser":
            ab = self._get_ab_session()
            msg = await ab.navigate(url)
            return {"backend": "agent-browser", "result": msg}
        if backend == "pilot":
            resp = await _helix_pilot_call("navigate", {"url": url}, url=self.helix_pilot_url)
            return {"backend": "helix-pilot", **resp}

        pw = self._get_pw_session()
        msg = await pw.navigate(url)
        return {"backend": "playwright", "result": msg}

    async def browse(self, url: str, task: str = "") -> dict:
        """High-level browse: open URL, extract text, return result.

        Args:
            url: Page URL to open.
            task: Optional task description (currently returns raw text).
        """
        backend = await self._resolve_backend()
        if backend == "none":
            return {"error": "No backend available. Install agent-browser, Playwright, or start helix-pilot."}

        if backend == "agent_browser":
            ab = self._get_ab_session()
            nav_msg = await ab.navigate(url)
            text = await ab.read_page()
            return {
                "backend": "agent-browser",
                "url": url,
                "task": task,
                "text": text,
                "navigation": nav_msg,
            }

        if backend != "playwright" and not HAS_PLAYWRIGHT:
            return {"error": "browse requires agent-browser or Playwright."}

        pw = self._get_pw_session()
        nav_msg = await pw.navigate(url)
        text = await pw.read_page()
        return {
            "backend": "playwright",
            "url": url,
            "task": task,
            "text": text,
            "navigation": nav_msg,
        }

    async def close(self) -> None:
        if self._ab_session:
            await self._ab_session.close()
            self._ab_session = None
        if self._pw_session:
            await self._pw_session.close()
            self._pw_session = None
