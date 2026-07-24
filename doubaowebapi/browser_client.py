"""Playwright-based Doubao client with in-browser fetch.

Architecture:
- Playwright: Login (QR scan via noVNC) + page session
- In-browser fetch(): API requests go through ByteDance's fetch hook which
  automatically injects a_bogus/msToken signatures with real browser fingerprint
- httpx: Only used for file upload (TOS/ImageX flow, no fetch hook needed)
- expose_function bridge: Streams SSE chunks from browser JS back to Python

ByteDance's frontend exposes window.bdms.frontierSign() which generates
X-Bogus signatures. We use Playwright only to maintain a logged-in page
and call this signing function. All actual API traffic goes through httpx.
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import urlencode

import httpx

# Patchright is a Playwright fork with built-in anti-detection patches:
# - Avoids Runtime.enable leak (major bot detection vector)
# - Disables Console.enable leak
# - Removes --enable-automation and other revealing flags
# - Drop-in replacement: same API as playwright
try:
    from patchright.async_api import BrowserContext, Page, async_playwright

    _USE_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import BrowserContext, Page, async_playwright

    _USE_PATCHRIGHT = False

log = logging.getLogger(__name__)

if _USE_PATCHRIGHT:
    log.info("Using Patchright (anti-detection Playwright fork)")
else:
    log.warning(
        "Patchright not installed, falling back to vanilla Playwright + stealth"
    )

DOUBAO_URL = "https://www.doubao.com"
CHAT_URL = f"{DOUBAO_URL}/chat/"
COMPLETION_URL = f"{DOUBAO_URL}/chat/completion"
SAMANTHA_COMPLETION_URL = f"{DOUBAO_URL}/samantha/chat/completion"
DEFAULT_BOT_ID = "7338286299411103781"


class BrowserClient:
    """Manages Playwright for login and in-browser fetch for API calls."""

    def __init__(self, headless: bool = True, user_data_dir: Optional[str] = None):
        self.headless = headless
        self.user_data_dir = user_data_dir
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._ready = False
        self._device_id: Optional[str] = None
        self._web_id: Optional[str] = None
        self._fp: Optional[str] = None
        # msToken rotation: updated from x-ms-token response header
        self._ms_token: str = ""
        # Robustness: failure tracking
        self._consecutive_failures: int = 0
        self._last_error_code: int = 0
        self._needs_captcha: bool = False
        # Stream bridge: request_id -> asyncio.Queue for SSE chunks
        self._stream_queues: Dict[str, asyncio.Queue] = {}
        self._bridge_ready: bool = False
        self._fetch_hook_confirmed: bool = False
        # Randomized bridge name to avoid detection of custom window properties
        self._bridge_name: str = f"_rc{uuid.uuid4().hex[:6]}"
        # Queue max size to prevent unbounded memory growth
        self._queue_maxsize: int = 500

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def needs_captcha(self) -> bool:
        return self._needs_captcha

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error_code(self) -> int:
        return self._last_error_code

    def record_success(self):
        """Reset failure counters on successful request."""
        self._consecutive_failures = 0
        self._last_error_code = 0
        self._needs_captcha = False

    def record_failure(self, error_code: int = 0):
        """Track consecutive failures. Mark captcha-needed on 710022004."""
        self._consecutive_failures += 1
        self._last_error_code = error_code
        if error_code == 710022004:
            self._needs_captcha = True
            log.warning("Captcha required (710022004) - marking needs_captcha=True")
        if self._consecutive_failures >= 5:
            log.error("5 consecutive failures - marking not ready")
            self._ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Connect to external browser via CDP, navigate to Doubao, init httpx client."""
        import os as _os

        cdp_url = _os.environ.get("DOUBAO_CDP_URL", "")
        if cdp_url:
            log.info("Connecting to external browser via CDP: %s", cdp_url)
            self._playwright = await async_playwright().start()
            browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    locale="zh-CN",
                )
            # Find an existing Doubao page or create a new one
            doubao_page = None
            for p in self._context.pages:
                if "doubao.com" in p.url:
                    doubao_page = p
                    break
            if doubao_page:
                self._page = doubao_page
                log.info("Reusing existing page: %s", doubao_page.url)
            else:
                self._page = await self._context.new_page()
                log.info("Navigating to %s", CHAT_URL)
                await self._page.goto(CHAT_URL, wait_until="load", timeout=60000)
                await asyncio.sleep(3)
        else:
            log.info("Starting BrowserClient (headless=%s)", self.headless)
            self._playwright = await async_playwright().start()
            # Patchright already removes --enable-automation and adds
            # --disable-blink-features=AutomationControlled, but we add
            # extra flags for stability and compatibility.
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--lang=zh-CN",
            ]
            if self.user_data_dir:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        self.user_data_dir,
                        headless=self.headless,
                        args=launch_args,
                        viewport={"width": 1280, "height": 720},
                        locale="zh-CN",
                    )
                )
                self._page = (
                    self._context.pages[0]
                    if self._context.pages
                    else await self._context.new_page()
                )
            else:
                browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=launch_args,
                )
                self._context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    locale="zh-CN",
                )
                self._page = await self._context.new_page()

            # playwright-stealth only needed with vanilla Playwright;
            # Patchright handles Runtime.enable / Console.enable leaks natively.
            try:
                from playwright_stealth import Stealth

                stealth = Stealth(navigator_languages_override=("zh-CN", "zh"))
                await stealth.apply_stealth_async(self._page)
                log.info("playwright-stealth patches applied")
            except ImportError:
                log.info(
                    "playwright-stealth not installed, relying on Patchright built-in stealth"
                )

        # Apply additional anti-detection patches (for both CDP and local modes)
        await self._apply_anti_detection_patches()

        # Navigate
        log.info("Navigating to %s", CHAT_URL)
        await self._page.goto(CHAT_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(3)

        # Init httpx
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10))

        # ── Auto-inject session cookies + localStorage from file ──
        await self._inject_session_from_file()

        await self._check_login_state()

    async def _apply_anti_detection_patches(self):
        """Apply additional anti-detection patches to evade bot detection.

        playwright-stealth covers the basics but misses some signals;
        this adds CDP-mode coverage, chrome.runtime, plugins, and removes
        Playwright-specific window properties that sites can probe.
        """
        if not self._page:
            return

        await self._page.evaluate("""
            () => {
                // 1. Remove navigator.webdriver
                try {
                    if (navigator.webdriver !== undefined) {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true,
                        });
                    }
                } catch(e) {}

                // 2. Mock chrome.runtime (real Chrome has this)
                try {
                    if (!window.chrome) window.chrome = {};
                    if (!window.chrome.runtime) {
                        window.chrome.runtime = {
                            id: undefined,
                            OnInstalledReason: {
                                CHROME_UPDATE: "chrome_update", INSTALL: "install",
                                SHARED_MODULE_UPDATE: "shared_module_update", UPDATE: "update",
                            },
                            OnRestartRequiredReason: {
                                APP_UPDATE: "app_update", OS_UPDATE: "os_update", PERIODIC: "periodic",
                            },
                            PlatformArch: {
                                ARM: "arm", ARM64: "arm64", X86_32: "x86-32", X86_64: "x86-64",
                            },
                            PlatformOs: {
                                ANDROID: "android", CROS: "cros", LINUX: "linux",
                                MAC: "mac", OPENBSD: "openbsd", WIN: "win",
                            },
                        };
                    }
                } catch(e) {}

                // 3. Mock plugins (headless Chrome has none)
                // Use try/catch — Patchright may have already defined this
                // as non-configurable via its own stealth patches
                try {
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            {0: {type: "application/pdf", suffixes: "pdf", description: "Portable Document Format"},
                             name: "Chrome PDF Plugin", filename: "internal-pdf-viewer",
                             description: "Portable Document Format", length: 1},
                            {0: {type: "application/x-google-chrome-pdf", suffixes: "pdf",
                                 description: "Portable Document Format"},
                             name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofoeelfocjilf",
                             description: "Portable Document Format", length: 1},
                            {0: {type: "application/x-nacl", suffixes: "", description: "Native Client Executable"},
                             name: "Native Client", filename: "internal-nacl-plugin",
                             description: "Native Client Executable", length: 2},
                        ],
                    });
                } catch(e) {}

                // 4. Mock Permissions API
                try {
                    if (navigator.permissions) {
                        const origQuery = navigator.permissions.query.bind(navigator.permissions);
                        navigator.permissions.query = (params) => {
                            if (params.name === 'notifications') {
                                return Promise.resolve({state: Notification.permission || 'default'});
                            }
                            return origQuery(params);
                        };
                    }
                } catch(e) {}

                // 5. Remove Playwright-specific window properties
                try {
                    delete window.__playwright;
                    delete window.__pw_manual;
                    delete window.__pw_script;
                } catch(e) {}

                // 6. Mock document.webdriver
                try {
                    Object.defineProperty(document, 'webdriver', {
                        get: () => undefined,
                        configurable: true,
                    });
                } catch(e) {}
            }
        """)
        log.info("Anti-detection patches applied")

    async def _inject_session_from_file(self):
        """Inject session cookies and localStorage from DOUBAO_SESSION_FILE.

        - Reads the session JSON file pointed to by DOUBAO_SESSION_FILE
          (env var, retains DOUBAO_ prefix for backwards compatibility but
          contains Doubao session data).
        - Injects each cookie into the browser context with domain .doubao.com.
        - Reads a sibling file ``doubao_localStorage.json`` (same directory as
          the session file) and injects its key/value pairs as localStorage
          on the current page.
        - Reloads the page so that the injected state takes effect before the
          login check runs.
        """
        if not self._context or not self._page:
            log.warning("inject_session: browser not started, skipping")
            return

        session_file = os.environ.get("DOUBAO_SESSION_FILE", "").strip()
        if not session_file:
            log.debug("inject_session: DOUBAO_SESSION_FILE not set, skipping")
            return

        session_path = Path(session_file)
        if not session_path.exists():
            log.info("inject_session: session file not found: %s", session_file)
            return

        # ── 1. Inject cookies ──
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("inject_session: failed to read %s: %s", session_file, e)
            return

        cookies = data.get("cookies", {}) if isinstance(data, dict) else {}
        if isinstance(cookies, dict) and cookies:
            pw_cookies = []
            for name, value in cookies.items():
                pw_cookies.append(
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": ".doubao.com",
                        "path": "/",
                        "sameSite": "Lax",
                    }
                )
            try:
                await self._context.add_cookies(pw_cookies)
                log.info(
                    "inject_session: injected %d cookies into browser context",
                    len(pw_cookies),
                )
            except Exception as e:
                log.warning("inject_session: failed to add cookies: %s", e)
        else:
            log.debug("inject_session: no cookies in session file")

        # ── 2. Inject localStorage ──
        ls_file = session_path.parent / "doubao_localStorage.json"
        if ls_file.exists():
            try:
                ls_data = json.loads(ls_file.read_text(encoding="utf-8"))
                if isinstance(ls_data, dict) and ls_data:
                    # Build a JS snippet that sets each localStorage key.
                    # Values are JSON-serialized to preserve types.
                    pairs = []
                    for k, v in ls_data.items():
                        pairs.append(
                            f"localStorage.setItem({json.dumps(str(k))}, "
                            f"{json.dumps(json.dumps(v, ensure_ascii=False))});"
                        )
                    js = "(() => { " + " ".join(pairs) + " })();"
                    await self._page.evaluate(js)
                    log.info(
                        "inject_session: injected %d localStorage entries from %s",
                        len(ls_data),
                        ls_file.name,
                    )
            except (OSError, json.JSONDecodeError) as e:
                log.warning(
                    "inject_session: failed to read localStorage %s: %s", ls_file, e
                )
            except Exception as e:
                log.warning("inject_session: failed to set localStorage: %s", e)
        else:
            log.debug(
                "inject_session: no doubao_localStorage.json next to session file"
            )

        # ── 3. Reload so injected state takes effect ──
        try:
            await self._page.reload(wait_until="load", timeout=30000)
            await asyncio.sleep(3)
            log.info("inject_session: page reloaded with injected session")
            # Re-apply anti-detection patches after reload (page navigation clears injected JS)
            await self._apply_anti_detection_patches()
        except Exception as e:
            log.warning("inject_session: reload failed: %s", e)

    async def stop(self):
        """Close browser and httpx client."""
        if self._http:
            await self._http.aclose()
            self._http = None
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._playwright = None
        self._page = None
        self._ready = False
        log.info("BrowserClient stopped")

    async def is_alive(self) -> bool:
        """Check if browser process is still responsive."""
        if not self._page or not self._context:
            return False
        try:
            result = await asyncio.wait_for(self._page.evaluate("1+1"), timeout=5)
            return result == 2
        except Exception as e:
            log.warning("Browser health check failed: %s", e)
            return False

    def _fail_all_pending_requests(self, reason: str = "browser restarted"):
        """Push an error signal to all pending stream queues so callers fail fast."""
        stale_ids = list(self._stream_queues.keys())
        for rid in stale_ids:
            queue = self._stream_queues.get(rid)
            if queue:
                try:
                    queue.put_nowait(f"__ERROR__:{reason}")
                except asyncio.QueueFull:
                    pass
        if stale_ids:
            log.warning("Failed %d pending requests: %s", len(stale_ids), reason)

    async def restart(self):
        """Stop and restart the browser client."""
        log.info("Restarting BrowserClient...")
        self._fail_all_pending_requests("browser restarting")
        await self.stop()
        await asyncio.sleep(2)
        await self.start()
        log.info("BrowserClient restarted. ready=%s", self._ready)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _check_login_state(self, retry_count: int = 2):
        """Check if logged in by looking for login button.

        Looks for both the Chinese (登录) and English (Log In) variants of the
        login button so the check works on Doubao's web UI regardless of locale.

        Args:
            retry_count: Number of retries if login button is still visible
                        (useful after page navigation during VNC login).
        """
        for attempt in range(retry_count + 1):
            # Use a single locator that matches both 登录 and Log In.
            # Do NOT use two locators — the English one is a subset of the
            # Chinese one (CSS ,selector = OR), so counting both double-counts
            # every "Log In" button.
            login_btn = self._page.locator(
                'button:has-text("登录"), button:has-text("Log In")'
            )
            btn_count = await login_btn.count()
            log.info(
                "Login check (attempt %d/%d): login_button_count=%d",
                attempt + 1,
                retry_count + 1,
                btn_count,
            )

            if btn_count == 0:
                # Logged in - extract params and setup bridge
                self._ready = True
                await self._extract_params()
                await self._seed_ms_token()
                await self._setup_fetch_bridge()
                await self._verify_fetch_hook()
                await self._wait_for_signing()  # still needed for upload endpoints
                log.info(
                    "Ready! device_id=%s, fetch_hook=%s",
                    self._device_id,
                    self._bridge_ready,
                )
                await self._discover_skills()
                return

            # Login button visible - might be transient after navigation
            if attempt < retry_count:
                log.info(
                    "Login button visible, waiting 2s for page to settle (attempt %d/%d)",
                    attempt + 1,
                    retry_count + 1,
                )
                await asyncio.sleep(2)
            else:
                log.info(
                    "Not logged in - login button still visible after %d attempts",
                    retry_count + 1,
                )
                self._ready = False
                return

    async def _extract_params(self):
        """Extract device_id, web_id, fp from localStorage/cookies."""
        for _ in range(5):
            params = await self._page.evaluate(
                """() => {
                const result = {};
                try {
                    const samWeb = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{}');
                    result.device_id = samWeb.web_id || '';
                } catch(e) {}
                try {
                    const tea = JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}');
                    result.web_id = tea.web_id || '';
                } catch(e) {}
                const fpCookie = document.cookie.split(';')
                    .map(c => c.trim())
                    .find(c => c.startsWith('s_v_web_id='));
                result.fp = fpCookie ? fpCookie.split('=')[1] : '';
                return result;
            }""",
                isolated_context=False,
            )
            self._device_id = params.get("device_id", "")
            self._web_id = params.get("web_id", "")
            self._fp = params.get("fp", "")
            if self._device_id and self._web_id:
                break
            await asyncio.sleep(1)
        log.info(
            "Params: device_id=%s, web_id=%s, fp=%s",
            self._device_id,
            self._web_id,
            self._fp[:20] if self._fp else "",
        )

    async def _wait_for_signing(self):
        """Wait for bdms.frontierSign to become available (legacy, kept for upload signing)."""
        for i in range(18):  # up to 90s
            has_sign = await self._page.evaluate(
                "() => typeof window.bdms?.frontierSign === 'function'",
                isolated_context=False,
            )
            if has_sign:
                log.info("bdms.frontierSign available after %ds", (i + 1) * 5)
                return
            await asyncio.sleep(5)
        log.warning(
            "bdms.frontierSign not available after %ds - signing may fail", (i + 1) * 5
        )

    async def _setup_fetch_bridge(self):
        """Register expose_function callback for streaming data from browser to Python."""
        if self._bridge_ready:
            return

        async def _on_stream_chunk(request_id: str, chunk_json: str):
            """Called from browser JS for each SSE chunk or completion signal."""
            queue = self._stream_queues.get(request_id)
            if queue:
                try:
                    queue.put_nowait(chunk_json)
                except asyncio.QueueFull:
                    # Drop oldest chunk to make room for new data
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(chunk_json)
                    except asyncio.QueueFull:
                        pass

        try:
            await self._page.expose_function(self._bridge_name, _on_stream_chunk)
            self._bridge_ready = True
            log.info("Fetch bridge registered (expose_function ready)")
        except Exception as e:
            # May already be registered if page didn't navigate
            if "already been registered" in str(e).lower():
                self._bridge_ready = True
                log.info("Fetch bridge already registered")
            else:
                log.error("Failed to register fetch bridge: %s", e)
                raise

    async def _verify_fetch_hook(self):
        """Verify ByteDance's fetch interceptor is active (adds a_bogus)."""
        for i in range(30):  # up to 60s
            hooked = await self._page.evaluate(
                """() => {
                try {
                    const s = window.fetch.toString();
                    return !s.includes('native code');
                } catch(e) { return false; }
            }""",
                isolated_context=False,
            )
            if hooked:
                log.info("Fetch hook verified active after %ds", (i + 1) * 2)
                self._fetch_hook_confirmed = True
                return True
            await asyncio.sleep(2)
        log.warning("Fetch hook NOT detected after 30s - requests may fail")
        return False

    async def wait_for_login(self, timeout: int = 120) -> bool:
        """Wait for user to scan QR code via noVNC."""
        await self._trigger_login_dialog()
        log.info("Waiting for QR scan login (timeout=%ds)...", timeout)
        try:
            login_btn = self._page.locator(
                'button:has-text("登录"), button:has-text("Log In")'
            )
            # Wait for login button to disappear (logged in)
            try:
                await login_btn.wait_for(state="hidden", timeout=timeout * 1000)
            except Exception:
                pass
            await asyncio.sleep(2)
            btn_count = await login_btn.count()
            if btn_count == 0:
                self._ready = True
                await self._extract_params()
                await self._seed_ms_token()
                await self._setup_fetch_bridge()
                await self._verify_fetch_hook()
                await self._wait_for_signing()
                log.info("Login successful!")
                return True
            return False
        except Exception as e:
            log.error("Login timeout: %s", e)
            return False

    async def _trigger_login_dialog(self):
        """Click login button to show QR code (supports 登录 and Log In)."""
        for selector in [
            'button:has-text("登录"), button:has-text("Log In")',
            'button:has-text("Log In")',
        ]:
            btn = self._page.locator(selector)
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(2)
                return

    async def inject_cookies_and_reload(self, cookies: Dict[str, str]) -> bool:
        """Inject cookies from QR login into browser context and reload.

        After qr_login.py obtains session cookies via pure HTTP,
        this method injects them into Playwright so that bdms.frontierSign
        becomes available.

        Returns True if login state is confirmed after reload.
        """
        if not self._context or not self._page:
            log.error("inject_cookies: browser not started")
            return False

        # Build cookie list for Playwright
        pw_cookies = []
        for name, value in cookies.items():
            pw_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".doubao.com",
                    "path": "/",
                    "sameSite": "Lax",
                }
            )

        await self._context.add_cookies(pw_cookies)
        log.info("Injected %d cookies into browser context", len(pw_cookies))

        # Reload page to pick up new session
        await self._page.reload(wait_until="load", timeout=30000)
        await asyncio.sleep(3)

        # Re-apply anti-detection patches after reload
        await self._apply_anti_detection_patches()

        # Re-check login state
        await self._check_login_state()
        return self._ready

    # ------------------------------------------------------------------
    # Signing & Cookies
    # ------------------------------------------------------------------

    async def _get_cookies_string(self) -> str:
        """Get full cookie string including httpOnly cookies."""
        cookies = await self._context.cookies("https://www.doubao.com")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async def _get_csrf_token(self) -> str:
        """Get passport_csrf_token from browser cookies."""
        cookies = await self._context.cookies("https://www.doubao.com")
        for c in cookies:
            if c["name"] == "passport_csrf_token":
                return c["value"]
            if c["name"] == "passport_csrf_token_default":
                return c["value"]
        return ""

    async def _seed_ms_token(self):
        """Seed initial msToken from browser cookies."""
        cookies = await self._context.cookies("https://www.doubao.com")
        for c in cookies:
            if c["name"] == "msToken":
                self._ms_token = c["value"]
                log.info("Seeded msToken from cookies (%d chars)", len(c["value"]))
                return
        log.warning("No msToken cookie found - first request may trigger rate limit")

    async def export_session_to_file(self, filepath: str) -> bool:
        """Export current browser cookies + localStorage + params to a session JSON file.

        Call this after VNC login to persist the session so it survives
        container restarts. Returns True on success.
        """
        try:
            cookies = await self._context.cookies("https://www.doubao.com")
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            # Also export localStorage keys needed for session restore
            ls_data = await self._page.evaluate("""() => {
                const result = {};
                const keys = [
                    'samantha_web_web_id',
                    '__tea_cache_tokens_497858',
                    '__tea_cache_tokens_497858'
                ];
                for (const k of keys) {
                    const v = localStorage.getItem(k);
                    if (v !== null) result[k] = v;
                }
                return result;
            }""")

            session_data = {
                "cookies": cookie_dict,
                "params": {
                    "device_id": self._device_id or "",
                    "web_id": self._web_id or "",
                    "fp": self._fp or "",
                },
            }

            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # Write localStorage to sibling file (read by _inject_session_from_file)
            ls_file = path.parent / "doubao_localStorage.json"
            ls_file.write_text(
                json.dumps(ls_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            log.info(
                "Exported session (%d cookies, %d localStorage keys) to %s",
                len(cookie_dict),
                len(ls_data),
                filepath,
            )
            return True
        except Exception as e:
            log.error("Failed to export session: %s", e)
            return False

    async def _sign_url(self, base_url: str, params: Dict[str, str]) -> str:
        """Sign a URL using bdms.frontierSign with retry on failure."""
        sorted_params = dict(sorted(params.items()))
        query_string = urlencode(sorted_params)

        last_error = None
        for attempt in range(3):
            try:
                # Use page.evaluate with argument passing instead of f-string
                # interpolation to avoid JS injection / encoding issues
                sig = await self._page.evaluate(
                    "([qs]) => window.bdms.frontierSign(qs)",
                    [query_string],
                    isolated_context=False,
                )

                a_bogus = ""
                if isinstance(sig, dict):
                    a_bogus = sig.get("a_bogus") or sig.get("X-Bogus", "")
                elif isinstance(sig, str):
                    a_bogus = sig

                if a_bogus:
                    return f"{base_url}?{query_string}&a_bogus={a_bogus}"

                last_error = f"empty signature: {sig}"
            except Exception as e:
                last_error = str(e)
                log.warning("frontierSign attempt %d failed: %s", attempt + 1, e)

            if attempt < 2:
                await asyncio.sleep(1)

        log.error("frontierSign failed after 3 attempts: %s", last_error)
        raise RuntimeError(f"Failed to generate a_bogus signature: {last_error}")

    def _build_query_params(self) -> Dict[str, str]:
        """Build the standard query parameters for API calls."""
        params = {
            "aid": "497858",
            "device_id": self._device_id or "",
            "device_platform": "web",
            "doubao_device_platform": "web",
            "doubao_pc_version": "3.28.7",
            "fp": self._fp or "",
            "language": "zh",
            "pc_version": "3.28.7",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "",
            "samantha_web": "1",
            "sys_region": "",
            "tea_uuid": self._web_id or "",
            "use-olympus-account": "1",
            "version_code": "20800",
            "web_id": self._web_id or "",
            "web_tab_id": str(uuid.uuid4()),
        }
        if self._ms_token:
            params["msToken"] = self._ms_token
        return params

    def _build_headers(self, cookie_str: str, csrf_token: str = "") -> Dict[str, str]:
        """Build request headers."""
        # Extract CSRF token from cookie string if not provided
        if not csrf_token:
            for part in cookie_str.split("; "):
                if part.startswith("passport_csrf_token="):
                    csrf_token = part.split("=", 1)[1]
                    break
        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Cookie": cookie_str,
            "Origin": DOUBAO_URL,
            "Referer": CHAT_URL,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "agw-js-conv": "str, str",
        }
        if csrf_token:
            headers["x-tt-passport-csrf-token"] = csrf_token
        return headers

    # ------------------------------------------------------------------
    # Chat Completion (streaming via in-browser fetch)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        text: str,
        conversation_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        use_deep_think: int = 0,
        image_attachments: Optional[list[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Send a chat message and yield SSE events via in-browser fetch.

        Args:
            image_attachments: Optional list of uploaded image metadata dicts
                (from upload_image), each with 'uri', 'cdn_url', 'format',
                'width', 'height' keys. Will be included as block_type:10052
                attachment_block in the payload.
        """
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        # Lazy check: if fetch hook wasn't confirmed at startup, re-verify now
        if not self._fetch_hook_confirmed:
            try:
                hooked = await self._page.evaluate(
                    "() => { try { const s = window.fetch.toString(); return !s.includes('native code'); } catch(e) { return false; } }",
                    isolated_context=False,
                )
                if hooked:
                    self._fetch_hook_confirmed = True
                    log.info("Fetch hook confirmed (lazy check) before chat request")
                else:
                    log.warning(
                        "Fetch hook still not active before chat request - may fail"
                    )
            except Exception as e:
                log.warning("Lazy fetch hook check failed: %s", e)

        need_create = conversation_id is None or conversation_id == ""
        effective_bot_id = bot_id or DEFAULT_BOT_ID
        msg_uuid = str(uuid.uuid4())
        local_conv_id = f"local_{uuid.uuid4().int % 10**16}"
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())

        payload = {
            "client_meta": {
                "local_conversation_id": local_conv_id if need_create else "",
                "conversation_id": conversation_id or "",
                "bot_id": effective_bot_id,
                "last_section_id": "",
                "last_message_index": None,
            },
            "messages": [
                {
                    "local_message_id": msg_uuid,
                    "content_block": [
                        *(  # attachment block for images
                            [
                                {
                                    "block_type": 10052,
                                    "content": {
                                        "attachment_block": {
                                            "attachments": [
                                                {
                                                    "type": 1,
                                                    "identifier": str(uuid.uuid4()),
                                                    "image": {
                                                        "uri": img.get("uri", ""),
                                                        "url": img.get("cdn_url", ""),
                                                        "width": int(
                                                            img.get("width", 100) or 100
                                                        ),
                                                        "height": int(
                                                            img.get("height", 100)
                                                            or 100
                                                        ),
                                                        "format": img.get(
                                                            "format", "png"
                                                        ),
                                                    },
                                                    "parse_state": 1,
                                                    "review_state": 1,
                                                    "upload_status": 1,
                                                    "progress": 100,
                                                }
                                                for img in (image_attachments or [])
                                            ]
                                        },
                                        "pc_event_block": "",
                                    },
                                    "block_id": str(uuid.uuid4()),
                                    "parent_id": "",
                                    "meta_info": [],
                                    "append_fields": [],
                                }
                            ]
                            if image_attachments
                            else []
                        ),
                        {
                            "block_type": 10000,
                            "content": {
                                "text_block": {
                                    "text": text,
                                    "icon_url": "",
                                    "icon_url_dark": "",
                                    "summary": "",
                                },
                                "pc_event_block": "",
                            },
                            "block_id": str(uuid.uuid4()),
                            "parent_id": "",
                            "meta_info": [],
                            "append_fields": [],
                        },
                    ],
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "tts_switch": False,
                "agent_mode": 2,
                "need_deep_think": use_deep_think,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": need_create,
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
                "is_old_user": True,
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": now_sec,
                    "append_sse_event_scene": 0,
                },
                "message_storage_type": 0,
            },
            "user_context": [],
            "ext": {
                "use_deep_think": str(use_deep_think),
                "fp": self._fp or "",
                "collection_id": "",
                "commerce_credit_config_enable": "0",
            },
        }

        # Build URL with query params (fetch hook will add a_bogus/msToken)
        query_params = self._build_query_params()
        query_string = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        url = f"/chat/completion?{query_string}"

        # Request jitter: random delay (120-360ms) to mimic human behavior
        # (borrowed from robinxplorer/doubao2API)
        jitter = random.uniform(0.12, 0.36)
        await asyncio.sleep(jitter)

        request_id = f"req_{uuid.uuid4().hex[:16]}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._stream_queues[request_id] = queue

        log.info(
            "POST %s (conv=%s, deep_think=%s) [browser fetch]",
            url.split("?")[0],
            conversation_id or "new",
            use_deep_think,
        )

        # Launch browser fetch in background
        eval_task = asyncio.create_task(
            self._browser_fetch_stream(url, payload, request_id)
        )

        # Yield parsed SSE events from queue
        try:
            while True:
                chunk_json = await asyncio.wait_for(queue.get(), timeout=180)
                if chunk_json is None:
                    # Stream complete
                    break
                if chunk_json.startswith("__ERROR__:"):
                    error_msg = chunk_json[10:]
                    log.error("Browser fetch error: %s", error_msg[:200])
                    yield {"error": True, "status": 0, "body": error_msg}
                    break
                if chunk_json.startswith("__HTTP_ERROR__:"):
                    status = int(chunk_json[15:].split(":", 1)[0])
                    body = (
                        chunk_json[15:].split(":", 1)[1]
                        if ":" in chunk_json[15:]
                        else ""
                    )
                    log.error("API error %d: %s", status, body[:200])
                    yield {"error": True, "status": status, "body": body}
                    break
                # Parse SSE line
                try:
                    data = json.loads(chunk_json)
                    # Check for Doubao business errors in SSE data
                    doubao_code = data.get("code", 0)
                    if doubao_code and doubao_code != 0:
                        doubao_msg = data.get("msg", "")
                        log.warning(
                            "Doubao SSE error code=%d msg=%s",
                            doubao_code,
                            doubao_msg[:200],
                        )
                        yield {
                            "error": True,
                            "status": 429 if doubao_code == 710022002 else 502,
                            "body": f"[Error code={doubao_code}: {doubao_msg}]",
                            "doubao_code": doubao_code,
                        }
                        self.record_failure(doubao_code)
                        break
                    yield data
                except json.JSONDecodeError:
                    continue
        except asyncio.TimeoutError:
            log.error("Stream timeout (180s) for request %s", request_id)
            yield {"error": True, "status": 0, "body": "Stream timeout"}
        finally:
            self._stream_queues.pop(request_id, None)
            if not eval_task.done():
                eval_task.cancel()
            else:
                # Check for exceptions
                try:
                    eval_task.result()
                except Exception:
                    pass

    async def _browser_fetch_stream(
        self, url: str, payload: Dict[str, Any], request_id: str
    ):
        """Execute fetch() inside browser page and stream SSE chunks via callback.

        Uses Patchright's isolated_context=True by default which avoids the
        Runtime.enable detection vector. The JS runs in an isolated execution
        context that is invisible to the page's own JavaScript.
        """
        bridge = self._bridge_name
        js_code = f"""
        async ([url, payloadJson, requestId]) => {{
            try {{
                // Ensure msToken is in URL (read from cookies if missing)
                if (!url.includes('msToken=')) {{
                    const msMatch = document.cookie.match(/(?:^|;\\s*)msToken=([^;]+)/);
                    if (msMatch) {{
                        url += '&msToken=' + msMatch[1];
                    }}
                }}
                const csrf = document.cookie.match(/passport_csrf_token=([^;]+)/);
                const csrfToken = csrf ? csrf[1] : '';
                const headers = {{
                    'Content-Type': 'application/json',
                    'Agw-Js-Conv': 'str, str',
                }};
                const res = await fetch(url, {{
                    method: 'POST',
                    headers: headers,
                    body: payloadJson,
                    credentials: 'include',
                }});
                if (!res.ok) {{
                    const errBody = await res.text();
                    await window.{bridge}(requestId,
                        '__HTTP_ERROR__:' + res.status + ':' + errBody.slice(0, 500));
                    return;
                }}
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let currentEvent = '';
                let buffer = '';
                while (true) {{
                    const {{done, value}} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {{stream: true}});
                    const lines = buffer.split('\\n');
                    buffer = lines.pop();
                    for (const line of lines) {{
                        const trimmed = line.trim();
                        if (!trimmed) continue;
                        if (trimmed.startsWith('event: ')) {{
                            currentEvent = trimmed.slice(7);
                            continue;
                        }}
                        if (trimmed.startsWith('id: ')) continue;
                        if (!trimmed.startsWith('data: ')) continue;
                        const dataStr = trimmed.slice(6);
                        if (!dataStr || dataStr === '{{}}') continue;
                        try {{
                            const obj = JSON.parse(dataStr);
                            obj._event = currentEvent;
                            await window.{bridge}(requestId, JSON.stringify(obj));
                        }} catch(e) {{}}
                    }}
                }}
                // Process remaining buffer
                if (buffer.trim()) {{
                    const trimmed = buffer.trim();
                    if (trimmed.startsWith('data: ')) {{
                        const dataStr = trimmed.slice(6);
                        if (dataStr && dataStr !== '{{}}') {{
                            try {{
                                const obj = JSON.parse(dataStr);
                                obj._event = currentEvent;
                                await window.{bridge}(requestId, JSON.stringify(obj));
                            }} catch(e) {{}}
                        }}
                    }}
                }}
                // Signal completion
                await window.{bridge}(requestId, null);
            }} catch(e) {{
                await window.{bridge}(requestId, '__ERROR__:' + e.message);
            }}
        }}
        """
        payload_json = json.dumps(payload, ensure_ascii=False)
        await self._page.evaluate(
            js_code, [url, payload_json, request_id], isolated_context=False
        )

    # ------------------------------------------------------------------
    # High-level chat helper
    # ------------------------------------------------------------------

    async def chat(
        self,
        text: str,
        conversation_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        use_deep_think: int = 0,
    ) -> Dict[str, Any]:
        """Send message, collect full response. Returns {text, conversation_id}."""
        full_text = ""
        result_conv_id = conversation_id
        events = []

        async for event in self.chat_completion(
            text,
            conversation_id=conversation_id,
            bot_id=bot_id,
            use_deep_think=use_deep_think,
        ):
            events.append(event)
            if event.get("error"):
                raise RuntimeError(
                    f"API error {event.get('status')}: {event.get('body', '')[:200]}"
                )
            if not result_conv_id:
                cid = self.extract_conversation_id(event)
                if cid and cid != "0":
                    result_conv_id = cid
            full_text += self._extract_text(event)

        return {"text": full_text, "conversation_id": result_conv_id}

    # ------------------------------------------------------------------
    # SSE parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(event: Dict[str, Any]) -> str:
        """Extract text content from a SSE event."""
        event_type = event.get("_event", "")

        if event_type == "CHUNK_DELTA" and "text" in event:
            return event["text"]

        if "patch_op" in event:
            for op in event["patch_op"]:
                pv = op.get("patch_value", {})
                for block in pv.get("content_block", []):
                    content = block.get("content", {})
                    tb = content.get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]
                if op.get("patch_object") == 102:
                    raw = pv.get("content", "")
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            if parsed.get("text"):
                                return parsed["text"]
                        except (json.JSONDecodeError, TypeError):
                            pass

        if event_type == "STREAM_MSG_NOTIFY":
            content = event.get("content", {})
            if isinstance(content, dict):
                for block in content.get("content_block", []):
                    tb = block.get("content", {}).get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]

        return ""

    @staticmethod
    def extract_conversation_id(event: Dict[str, Any]) -> Optional[str]:
        """Extract conversation_id from SSE events."""
        ack = event.get("ack_client_meta", {})
        if ack.get("conversation_id"):
            return ack["conversation_id"]
        meta = event.get("meta", {})
        if meta.get("conversation_id"):
            return meta["conversation_id"]
        return None

    # ------------------------------------------------------------------
    # Samantha endpoint (image/video/music generation)
    # ------------------------------------------------------------------

    async def _samantha_request(
        self,
        payload: Dict[str, Any],
        timeout: float = 120,
    ) -> str:
        """Send a request to /samantha/chat/completion via in-browser fetch."""
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        query_params = self._build_query_params()
        query_string = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        url = f"/samantha/chat/completion?{query_string}"

        js_code = """
        async ([url, payloadJson, timeoutMs]) => {
            // Ensure msToken is in URL (read from cookies if missing)
            if (!url.includes('msToken=')) {
                const msMatch = document.cookie.match(/(?:^|;\s*)msToken=([^;]+)/);
                if (msMatch) {
                    url += '&msToken=' + msMatch[1];
                }
            }
            const csrf = document.cookie.match(/passport_csrf_token=([^;]+)/);
            const csrfToken = csrf ? csrf[1] : '';
            const headers = {
                'Content-Type': 'application/json',
                'Agw-Js-Conv': 'str, str',
            };
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const res = await fetch(url, {
                    method: 'POST',
                    headers: headers,
                    body: payloadJson,
                    credentials: 'include',
                    signal: controller.signal,
                });
                clearTimeout(timer);
                if (!res.ok) {
                    const errBody = await res.text();
                    return {error: true, status: res.status, body: errBody.slice(0, 500)};
                }
                const body = await res.text();
                return {error: false, body: body};
            } catch(e) {
                clearTimeout(timer);
                return {error: true, status: 0, body: e.message};
            }
        }
        """
        payload_json = json.dumps(payload, ensure_ascii=False)
        timeout_ms = int(timeout * 1000)

        log.info("POST %s [browser fetch, timeout=%ds]", url.split("?")[0], timeout)
        result = await self._page.evaluate(
            js_code, [url, payload_json, timeout_ms], isolated_context=False
        )

        if result.get("error"):
            status = result.get("status", 0)
            body = result.get("body", "")
            raise RuntimeError(
                f"samantha/chat/completion failed ({status}): {body[:500]}"
            )

        body = result.get("body", "")
        if body.lstrip().startswith("{"):
            try:
                err = json.loads(body)
                if isinstance(err, dict) and "code" in err:
                    raise RuntimeError(
                        f"samantha auth error: code={err.get('code')} msg={err.get('msg') or err.get('message', '')}"
                    )
            except json.JSONDecodeError:
                pass
        return body

    @staticmethod
    def _parse_samantha_sse(raw: str) -> List[Dict[str, Any]]:
        """Parse samantha SSE body into list of event dicts."""
        events = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                events.append(json.loads(data_str))
            except json.JSONDecodeError:
                continue
        return events

    async def generate_image(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        ref_image_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate images using /samantha/chat/completion.

        Args:
            prompt: Text description of the image to generate.
            ratio: Aspect ratio ("1:1", "16:9", "9:16", "4:3", "3:4").
            ref_image_key: Optional uploaded image key for reference.

        Returns:
            Dict with 'images' list, each having url/width/height/key.
        """
        content_data: Dict[str, Any] = {"text": prompt}
        if ratio:
            content_data["ratio"] = ratio

        message: Dict[str, Any] = {
            "content": json.dumps(content_data, ensure_ascii=False),
            "content_type": 2009,
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 3,
                "skill_type_no_default": 3,
                "skill_id": "3",
                "skill_id_no_default": "3",
            },
        }

        if ref_image_key:
            message["attachments"] = [
                {
                    "type": "image",
                    "key": ref_image_key,
                    "extra": {"refer_types": "overall"},
                }
            ]

        payload = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "is_delete": False,
                "is_ai_playground": False,
                "is_old_user": True,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 3,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        log.info("generate_image: prompt=%s, ratio=%s", prompt[:50], ratio)
        raw = await self._samantha_request(payload, timeout=120)

        # Parse response - look for content_type=2010 (image output)
        images = []
        for data in self._parse_samantha_sse(raw):
            et = data.get("event_type")
            if et == 2005:
                detail = data.get("event_data", "")
                raise RuntimeError(f"generate_image error: {str(detail)[:500]}")
            if et != 2001:
                continue

            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue

            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue

            if msg.get("content_type") != 2010:
                continue

            content_raw = msg.get("content", "")
            if isinstance(content_raw, str):
                try:
                    content = json.loads(content_raw)
                except json.JSONDecodeError:
                    continue
            else:
                content = content_raw

            for item in content.get("data", []):
                if not isinstance(item, dict):
                    continue
                ori = item.get("image_ori", {}) or {}
                raw_img = item.get("image_raw", {}) or {}
                thumb = item.get("image_thumb", {}) or {}
                images.append(
                    {
                        "key": item.get("key", ""),
                        "url": ori.get("url")
                        or raw_img.get("url")
                        or thumb.get("url", ""),
                        "width": ori.get("width") or thumb.get("width", 0),
                        "height": ori.get("height") or thumb.get("height", 0),
                        "format": ori.get("format") or thumb.get("format", ""),
                    }
                )

        log.info("generate_image: got %d images", len(images))
        return {"images": images, "prompt": prompt}

    async def generate_image_via_page(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        style: Optional[str] = None,
        timeout: float = 120,
        image_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate images by driving the real Doubao web UI.

        The /samantha/chat/completion endpoint used by the legacy generate_image
        is deprecated (returns 504). The new /chat/completion endpoint requires
        an a_bogus signature that is bound to the request body, so programmatic
        fetch is rejected (710020202 invalid param). Only the page's own
        frontend can issue a valid request because it signs the body it just
        constructed.

        This method works around that by simulating a real user: open a fresh
        chat, switch to the image skill, type the prompt, press Enter, then
        poll the DOM for newly inserted rc_gen_image <img> elements. The
        captured URLs carry x-signature params and are directly downloadable.

        Args:
            prompt: Text description of the image to generate.
            ratio: Aspect ratio hint ("1:1","2:3","3:4","4:3","9:16","16:9").
                NOTE: applying ratio/style requires driving the dropdown UI,
                which is currently unreliable; the value is recorded but may
                not take effect. The default (1:1) is used when selection fails.
            style: Style name hint (e.g. "动漫","油画"). Same caveat as ratio.
            timeout: Max seconds to wait for images to appear.
            image_model: Specific model name (e.g. "seedream-5.0-pro").

        Returns:
            Dict with 'images' list (each {url,width,height,key}) and 'prompt'.
        """
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        # Open a fresh chat page so we don't disturb the user's main conversation
        # and so history images don't pollute the result.
        ctx = self._context
        page = await ctx.new_page()
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

            # Snapshot pre-existing generated-image URLs so we can filter them out
            before_imgs = set(
                await page.evaluate(
                    "() => [...document.querySelectorAll('img')]"
                    ".map(i => i.src).filter(s => s.includes('rc_gen_image'))"
                )
            )

            # Switch to image skill (skill_bar_button_3 = 图像生成)
            try:
                await page.locator('[data-skill-id="skill_bar_button_3"]').click(
                    timeout=8000
                )
                await asyncio.sleep(2.0)
            except Exception as e:
                log.warning("generate_image_via_page: skill button click failed: %s", e)

            # NOTE: ratio/style/model selection via dropdown is unreliable
            # (the popover is hover/portal-based and resists programmatic
            # click). We attempt it but do not block on failure — the
            # default ratio (1:1) and model are acceptable.
            if image_model:
                await self._try_select_dropdown(page, "模型", image_model)
            if ratio:
                await self._try_select_dropdown(page, "比例", ratio)
            if style:
                await self._try_select_dropdown(page, "风格", style)

            # Type the prompt and send via Enter (mimics a real user, so the
            # frontend signs the resulting body itself).
            try:
                loc = page.locator(
                    'textarea:visible, [contenteditable="true"]:visible'
                ).first
                await loc.click(timeout=8000)
            except Exception:
                await page.focus("textarea")
            await page.keyboard.type(prompt, delay=30)
            await asyncio.sleep(0.8)
            await page.keyboard.press("Enter")

            # Poll the DOM for newly inserted rc_gen_image URLs (generated images
            # are served from *.ibyteimg.com/.../rc_gen_image/... with x-signature).
            new_imgs: list[str] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(2)
                cur = set(
                    await page.evaluate(
                        "() => [...document.querySelectorAll('img')]"
                        ".map(i => i.src).filter(s => s.includes('rc_gen_image'))"
                    )
                )
                fresh = cur - before_imgs
                if fresh:
                    new_imgs = list(fresh)
                    # Wait one extra cycle to collect all images in the batch
                    await asyncio.sleep(4)
                    cur = set(
                        await page.evaluate(
                            "() => [...document.querySelectorAll('img')]"
                            ".map(i => i.src).filter(s => s.includes('rc_gen_image'))"
                        )
                    )
                    new_imgs = list(cur - before_imgs)
                    break

            if not new_imgs:
                log.warning(
                    "generate_image_via_page: no images captured within %.0fs", timeout
                )
            else:
                log.info("generate_image_via_page: captured %d images", len(new_imgs))

            images = [
                {
                    "url": u,
                    "key": "",
                    "width": 0,
                    "height": 0,
                    "format": "png",
                }
                for u in new_imgs
            ]
            return {"images": images, "prompt": prompt}
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _discover_skills(self):
        """Log all available skill buttons on the current page for diagnostics."""
        try:
            skills = await self._page.evaluate(
                """() => {
                return Array.from(document.querySelectorAll('[data-skill-id]')).map(el => ({
                    id: el.getAttribute('data-skill-id'),
                    text: (el.textContent||'').trim().slice(0, 40)
                }));
            }""",
                isolated_context=False,
            )
            log.info(
                "Doubao web UI skills discovered: %s",
                json.dumps(skills, ensure_ascii=False),
            )
        except Exception as e:
            log.warning("_discover_skills failed: %s", e)

    async def _try_select_dropdown(self, page: Page, label: str, value: str) -> bool:
        """Best-effort attempt to open a skill dropdown and pick an option.

        The Doubao web UI renders ratio/style selectors as hover/portal popovers
        that are hard to drive programmatically. This helper tries several
        strategies but never raises — selection failure simply falls back to
        the default value.
        """
        try:
            # Strategy 1: click the label row container, then pick from popover
            await page.evaluate(
                """(label) => {
                    const rows = Array.from(document.querySelectorAll('div'));
                    const row = rows.find(el =>
                        el.className && el.className.includes('flex min-w-0 items-center gap-4')
                        && (el.textContent||'').includes(label));
                    if (row) row.click();
                }""",
                label,
            )
            await asyncio.sleep(1.5)

            # Strategy 2: hover the label span to trigger a popover
            try:
                await page.get_by_text(label, exact=True).first.hover(timeout=2000)
                await asyncio.sleep(1.2)
            except Exception:
                pass

            # Strategy 3: click the current value button to open the popover
            try:
                # After clicking the row, a popover should appear. Look for
                # the currently-selected value button within the row and click it.
                await page.evaluate(
                    """(label) => {
                        const rows = Array.from(document.querySelectorAll('div'));
                        const row = rows.find(el =>
                            el.className && el.className.includes('flex min-w-0 items-center gap-4')
                            && (el.textContent||'').includes(label));
                        if (row) {
                            // Click the button-like child that shows the current value
                            const btns = row.querySelectorAll('div');
                            for (const b of btns) {
                                if (b.textContent && b.textContent.includes(':') && b.offsetWidth > 0) {
                                    b.click();
                                    return;
                                }
                            }
                        }
                    }""",
                    label,
                )
                await asyncio.sleep(0.8)
            except Exception:
                pass

            # Try to click the target option. Match either the bare value
            # (e.g. "2:3") or a value-prefixed description (e.g. "2:3 社交媒体").
            picked = await page.evaluate(
                """(value) => {
                    // Try first with exact match on leaf nodes
                    const opts = Array.from(document.querySelectorAll('*'));
                    for (const el of opts) {
                        const t = (el.textContent||'').trim();
                        if (el.children.length === 0 && (t === value || t.startsWith(value + ' '))) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.top > 100) {
                                let target = el;
                                for (let i=0;i<4 && target.parentElement; i++) target = target.parentElement;
                                target.click();
                                return true;
                            }
                        }
                    }
                    // Fallback: try matching any element whose text starts with the value
                    for (const el of opts) {
                        const t = (el.textContent||'').trim();
                        if (t.startsWith(value + ' ') || t === value) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.top > 100) {
                                let target = el;
                                for (let i=0;i<6 && target.parentElement; i++) target = target.parentElement;
                                target.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""",
                value,
            )
            if picked:
                await asyncio.sleep(0.8)
                await page.keyboard.press("Escape")
                log.info("dropdown select (%s=%s) succeeded", label, value)
                return True
            await page.keyboard.press("Escape")
            log.warning(
                "dropdown select (%s=%s) failed — option not found in popover",
                label,
                value,
            )
            return False
        except Exception as e:
            log.debug("dropdown select (%s=%s) failed: %s", label, value, e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def generate_music(
        self,
        prompt: str,
        lyric: Optional[str] = None,
        genre: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate music using /samantha/chat/completion.

        Args:
            prompt: Text description of the music to generate.
            lyric: Explicit lyrics (optional).
            genre: Music genre (optional).

        Returns:
            Dict with 'tracks' list, each having audio_url/title/lyrics/duration.
        """
        import base64

        content_data: Dict[str, Any] = {"text": prompt}
        if lyric:
            content_data["lyric"] = lyric
        if genre:
            content_data["genre"] = genre

        message: Dict[str, Any] = {
            "content": json.dumps(content_data, ensure_ascii=False),
            "content_type": 2005,
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 9,
                "skill_type_no_default": 9,
                "skill_id": "9",
                "skill_id_no_default": "9",
            },
        }

        payload = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "is_delete": False,
                "is_ai_playground": False,
                "is_old_user": True,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 9,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        log.info("generate_music: prompt=%s", prompt[:50])
        raw = await self._samantha_request(payload, timeout=300)

        # Parse: find last content_type=2006 with video_model
        tracks = []
        final_content = None
        for data in self._parse_samantha_sse(raw):
            et = data.get("event_type")
            if et == 2005:
                detail = data.get("event_data", "")
                raise RuntimeError(f"generate_music error: {str(detail)[:500]}")
            if et != 2001:
                continue

            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue

            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue

            if msg.get("content_type") not in (2006, 2004):
                continue

            content_raw = msg.get("content", "")
            if isinstance(content_raw, str):
                try:
                    content = json.loads(content_raw)
                except json.JSONDecodeError:
                    continue
            else:
                content = content_raw

            # Keep updating - we want the final (most complete) version
            final_content = content

        if not final_content:
            log.warning("generate_music: no content_type=2006 found")
            return {"tracks": [], "prompt": prompt}

        # Parse tasks
        tasks = final_content.get("tasks", {})
        if isinstance(tasks, dict):
            tasks_list = list(tasks.values())
        elif isinstance(tasks, list):
            tasks_list = tasks
        else:
            tasks_list = []

        for task in tasks_list:
            if not isinstance(task, dict):
                continue

            audio_url = ""
            duration = 0.0
            vm_str = task.get("video_model", "")
            if vm_str:
                try:
                    vm = json.loads(vm_str) if isinstance(vm_str, str) else vm_str
                    duration = vm.get("video_duration", 0.0)
                    vlist = vm.get("video_list", {})
                    for _q, vinfo in vlist.items():
                        main_url_b64 = vinfo.get("main_url", "")
                        if main_url_b64:
                            audio_url = base64.b64decode(main_url_b64).decode(
                                "utf-8", errors="replace"
                            )
                            break
                except (json.JSONDecodeError, Exception):
                    pass

            cover_url = ""
            cover = task.get("cover", {})
            if isinstance(cover, dict):
                cover_ori = cover.get("image_ori", {}) or {}
                cover_url = cover_ori.get("url", "")

            if audio_url or task.get("title"):
                tracks.append(
                    {
                        "audio_url": audio_url,
                        "title": task.get("title", ""),
                        "lyrics": task.get("lyric", ""),
                        "duration": duration,
                        "cover_url": cover_url,
                    }
                )

        log.info("generate_music: got %d tracks", len(tracks))
        return {"tracks": tracks, "prompt": prompt}

    async def generate_video(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate video using /samantha/chat/completion (async 2-step).

        Args:
            prompt: Text description of the video to generate.
            ratio: Aspect ratio ("16:9", "9:16", "1:1").
            duration: Video duration in seconds ("5" or "10").

        Returns:
            Dict with 'videos' list, each having video_url/cover_url/duration.
        """

        content_data: Dict[str, Any] = {"text": prompt}
        if ratio:
            content_data["ratio"] = ratio
        if duration:
            content_data["duration"] = int(duration)

        message: Dict[str, Any] = {
            "content": json.dumps(content_data, ensure_ascii=False),
            "content_type": 2020,
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 17,
                "skill_type_no_default": 17,
                "skill_id": "17",
                "skill_id_no_default": "17",
            },
        }

        payload = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "is_delete": False,
                "is_ai_playground": False,
                "is_old_user": True,
                "memory_type": 2,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 17,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        log.info("generate_video: prompt=%s, ratio=%s", prompt[:50], ratio)
        raw = await self._samantha_request(payload, timeout=60)

        # Phase 1: Extract async task_id from fin_reason
        task_id = None
        text_parts = []
        for data in self._parse_samantha_sse(raw):
            et = data.get("event_type")
            if et == 2005:
                detail = data.get("event_data", "")
                raise RuntimeError(f"generate_video error: {str(detail)[:500]}")
            if et != 2001:
                continue

            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue

            # Check for async task
            fin_reason = ed.get("fin_reason", {})
            if fin_reason and fin_reason.get("reason") == 1:
                async_task = fin_reason.get("async_task", {})
                task_id = async_task.get("id", "")

            # Collect text for error messages
            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue
            if msg.get("content_type") == 2001:
                content_raw = msg.get("content", "")
                if isinstance(content_raw, str):
                    try:
                        c = json.loads(content_raw)
                        text_parts.append(c.get("text", ""))
                    except json.JSONDecodeError:
                        pass

        full_text = "".join(text_parts)
        if "服务过载" in full_text or "重试" in full_text:
            raise RuntimeError("视频生成服务过载，请稍后重试")

        if not task_id:
            # Maybe sync result with content_type=2021, or just text response
            if full_text:
                return {"videos": [], "prompt": prompt, "message": full_text}
            raise RuntimeError("Video generation: no task_id returned")

        # Phase 2: Poll for result
        log.info("generate_video: polling task_id=%s", task_id)
        return await self._poll_video_result(task_id, prompt)

    async def _poll_video_result(
        self, task_id: str, prompt: str, timeout: float = 300
    ) -> Dict[str, Any]:
        """Poll /samantha/chat/completion with task_id for video result."""
        import base64

        poll_payload = {"task_id": task_id, "event_id": 0}
        # Use _samantha_request which now uses browser fetch
        raw = await self._samantha_request(poll_payload, timeout=timeout)

        videos = []
        for data in self._parse_samantha_sse(raw):
            et = data.get("event_type")
            if et != 2001:
                continue

            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue

            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue

            if msg.get("content_type") != 2021:
                continue

            content_raw = msg.get("content", "")
            if isinstance(content_raw, str):
                try:
                    content = json.loads(content_raw)
                except json.JSONDecodeError:
                    continue
            else:
                content = content_raw

            for item in content.get("data", [content]):
                if not isinstance(item, dict):
                    continue
                video_url = item.get("video_url", "") or item.get("url", "")
                if not video_url:
                    vm_str = item.get("video_model", "")
                    if vm_str:
                        try:
                            vm = (
                                json.loads(vm_str)
                                if isinstance(vm_str, str)
                                else vm_str
                            )
                            vlist = vm.get("video_list", {})
                            for _q, vinfo in vlist.items():
                                main_b64 = vinfo.get("main_url", "")
                                if main_b64:
                                    video_url = base64.b64decode(main_b64).decode(
                                        "utf-8", errors="replace"
                                    )
                                    break
                        except (json.JSONDecodeError, Exception):
                            pass

                cover_url = item.get("cover_url", "") or item.get("cover", {}).get(
                    "url", ""
                )
                if video_url:
                    videos.append(
                        {
                            "video_url": video_url,
                            "cover_url": cover_url,
                            "width": item.get("width", 0),
                            "height": item.get("height", 0),
                            "duration": item.get("duration", 0.0),
                        }
                    )

        log.info("generate_video: got %d videos", len(videos))
        return {"videos": videos, "prompt": prompt}

    # ------------------------------------------------------------------
    # File upload (TOS / ImageX flow)
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        file_data: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """Upload a file to Doubao's storage via browser-native ImageX SDK.

        Instead of using prepare_upload (which returns 710010703 on doubao.com),
        we trigger the Doubao frontend's own upload flow by setting the file
        input and capturing the resulting StoreUri from the ApplyImageUpload
        response. The frontend handles ApplyImageUpload → TOS upload →
        CommitImageUpload automatically.

        Returns:
            Dict with uri, name, size, file_type.
        """
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        file_size = len(file_data)

        # Capture StoreUri from the browser's ApplyImageUpload response
        store_uris: list[str] = []

        async def _capture_uri(response):
            if "ApplyImageUpload" in response.url:
                try:
                    body = await response.json()
                    infos = (
                        body.get("Result", {})
                        .get("UploadAddress", {})
                        .get("StoreInfos", [])
                    )
                    for si in infos:
                        uri = si.get("StoreUri", "")
                        if uri:
                            store_uris.append(uri)
                except Exception:
                    pass

        self._page.on("response", _capture_uri)
        try:
            # Write the file data to a temp file inside the container,
            # then use patchright's set_input_files which triggers the
            # native <input type="file"> change event.  This is more
            # reliable than the React __reactProps hack because Doubao's
            # frontend listens for the real 'change' event to start the
            # ApplyImageUpload → TOS → CommitImageUpload flow.
            import os
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                suffix=f".{ext}", delete=False, dir="/tmp"
            )
            tmp.write(file_data)
            tmp.close()

            try:
                file_input = self._page.locator('input[type="file"]').first
                await file_input.set_input_files(tmp.name)
            finally:
                os.unlink(tmp.name)

            # Wait for the upload to complete (frontend does 3 HTTP calls)
            for _ in range(30):
                await asyncio.sleep(0.5)
                if store_uris:
                    break

            if not store_uris:
                raise RuntimeError("File upload timed out - no StoreUri received")

            # Clear the pending attachment from Doubao's UI to prevent it
            # being sent as a message on the next user interaction
            try:
                await self._page.evaluate("""() => {
                    // Find and click any remove/close buttons on pending attachments
                    const btns = document.querySelectorAll('[class*="remove"], [class*="close"], [class*="delete"]');
                    for (const btn of btns) {
                        if (btn.offsetParent !== null) btn.click();
                    }
                    // Also try clearing the textarea to dismiss any upload UI
                    const ta = document.querySelector('textarea');
                    if (ta) { ta.value = ''; ta.dispatchEvent(new Event('input', {bubbles:true})); }
                }""")
            except Exception:
                pass
        finally:
            self._page.remove_listener("response", _capture_uri)

        store_uri = store_uris[0]
        log.info("File uploaded via browser: %s -> %s", filename, store_uri)
        return {"uri": store_uri, "name": filename, "size": file_size, "file_type": ext}

    async def get_file_download_url(
        self,
        uri: str,
        expire_seconds: int = 3600,
    ) -> Dict[str, Any]:
        """Get a temporary CDN URL for a previously uploaded file."""
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")
        query_params = self._build_query_params()
        signed_url = await self._sign_url(
            f"{DOUBAO_URL}/alice/message/get_file_url", query_params
        )
        cookie_str = await self._get_cookies_string()
        headers = self._build_headers(cookie_str)
        ext = uri.rsplit(".", 1)[-1] if "." in uri else ""
        resp = await self._http.post(
            signed_url,
            headers=headers,
            json={
                "uris": [uri],
                "type": "file",
                "format": ext,
                "expire_second": expire_seconds,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"get_file_url failed ({resp.status_code}): {resp.text[:500]}"
            )
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"get_file_url error: {body.get('msg', body)}")
        file_urls = body.get("data", {}).get("file_urls", [])
        if not file_urls:
            raise RuntimeError("get_file_url returned no file_urls")
        return file_urls[0].get("main_url", "")

    async def upload_image(
        self,
        image_bytes: bytes,
        filename: str = "image.png",
    ) -> Dict[str, Any]:
        """Upload an image via browser-native ImageX SDK for vision.

        Uses the same browser-native upload path as the Doubao frontend:
        triggers file input → ApplyImageUpload → TOS upload → CommitImageUpload.
        This produces ``tos-mya-i-*`` URIs that the chat/completion API recognises,
        unlike the legacy ``/samantha/pages/upload_image`` endpoint which returns
        ``pages_upload_image_*`` URIs that Doubao's model cannot see.

        Returns dict with: uri, cdn_url, format, width, height.
        """
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"

        # Determine image dimensions from bytes
        width, height = 100, 100
        try:
            import io as _io

            from PIL import Image as _PILImage

            img = _PILImage.open(_io.BytesIO(image_bytes))
            width, height = img.size
        except Exception:
            pass  # fallback to defaults

        # Reuse upload_file (browser-native ImageX path)
        upload_result = await self.upload_file(image_bytes, filename)
        uri = upload_result.get("uri", "")
        if not uri:
            raise RuntimeError(
                f"Image upload via browser returned no uri: {upload_result}"
            )

        # Get CDN URL via get_file_url
        cdn_url = ""
        try:
            query_params2 = self._build_query_params()
            file_url = await self._sign_url(
                f"{DOUBAO_URL}/alice/message/get_file_url", query_params2
            )
            cookie_str = await self._get_cookies_string()
            headers = self._build_headers(cookie_str)
            resp = await self._http.post(
                file_url,
                headers=headers,
                json={
                    "uris": [uri],
                    "type": "image",
                    "format": ext,
                    "expire_second": 3600,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                fb = resp.json()
                if fb.get("code") == 0:
                    file_urls = fb.get("data", {}).get("file_urls", [])
                    if file_urls:
                        cdn_url = file_urls[0].get("main_url", "")
        except Exception as e:
            log.warning("get_file_url failed for image %s: %s", uri, e)

        return {
            "uri": uri,
            "cdn_url": cdn_url,
            "name": filename,
            "format": ext,
            "width": width,
            "height": height,
        }

    async def chat_with_file(
        self,
        text: str,
        file_uri: str,
        file_name: str,
        file_size: int,
        use_deep_think: int = 0,
    ) -> Dict[str, Any]:
        """Chat with a file attachment. The AI will read the file and answer.

        Args:
            text: Question about the file.
            file_uri: URI from upload_file().
            file_name: Original filename.
            file_size: File size in bytes.
            use_deep_think: 0=quick, 1=think, 3=expert.

        Returns:
            Dict with 'text' and 'conversation_id'.
        """
        if not self._ready:
            raise RuntimeError("Browser not ready - need login first")

        msg_uuid = str(uuid.uuid4())
        local_conv_id = f"local_{uuid.uuid4().int % 10**16}"
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())

        if isinstance(file_uri, list):
            file_refs = file_uri
        else:
            file_refs = [{"uri": file_uri, "name": file_name, "size": file_size}]
        file_attachments = []
        for file_ref in file_refs:
            file_attachments.append(
                {
                    "type": 3,
                    "identifier": str(uuid.uuid4()),
                    "file": {
                        "uri": file_ref.get("uri", ""),
                        "url": "",
                        "file_type": 0,
                        "name": file_ref.get("name", "file.txt"),
                        "size": int(file_ref.get("size") or 0),
                    },
                    "parse_state": 1,
                    "review_state": 1,
                    "upload_status": 1,
                    "progress": 100,
                    "src": "",
                }
            )

        payload = {
            "client_meta": {
                "local_conversation_id": local_conv_id,
                "conversation_id": "",
                "bot_id": DEFAULT_BOT_ID,
                "last_section_id": "",
                "last_message_index": None,
            },
            "messages": [
                {
                    "local_message_id": msg_uuid,
                    "content_block": [
                        {
                            "block_type": 10052,
                            "content": {
                                "attachment_block": {"attachments": file_attachments},
                                "pc_event_block": "",
                            },
                            "block_id": str(uuid.uuid4()),
                            "parent_id": "",
                            "meta_info": [],
                            "append_fields": [],
                        },
                        {
                            "block_type": 10000,
                            "content": {
                                "text_block": {
                                    "text": text,
                                    "icon_url": "",
                                    "icon_url_dark": "",
                                    "summary": "",
                                },
                                "pc_event_block": "",
                            },
                            "block_id": str(uuid.uuid4()),
                            "parent_id": "",
                            "meta_info": [],
                            "append_fields": [],
                        },
                    ],
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "tts_switch": False,
                "agent_mode": 2,
                "need_deep_think": use_deep_think,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": True,
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
                "is_old_user": True,
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": now_sec,
                    "append_sse_event_scene": 0,
                },
                "message_storage_type": 0,
            },
            "ext": {
                "use_deep_think": str(use_deep_think),
                "fp": self._fp or "",
                "collection_id": "",
                "commerce_credit_config_enable": "0",
            },
        }

        # Build URL with query params (fetch hook will add a_bogus/msToken)
        query_params = self._build_query_params()
        query_string = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        url = f"/chat/completion?{query_string}"

        # Use browser fetch (non-streaming, collect full response)
        js_code = """
        async ([url, payloadJson]) => {
            // Ensure msToken is in URL (read from cookies if missing)
            if (!url.includes('msToken=')) {
                const msMatch = document.cookie.match(/(?:^|;\s*)msToken=([^;]+)/);
                if (msMatch) {
                    url += '&msToken=' + msMatch[1];
                }
            }
            const csrf = document.cookie.match(/passport_csrf_token=([^;]+)/);
            const csrfToken = csrf ? csrf[1] : '';
            const headers = {
                'Content-Type': 'application/json',
                'Agw-Js-Conv': 'str, str',
            };
            const res = await fetch(url, {
                method: 'POST',
                headers: headers,
                body: payloadJson,
                credentials: 'include',
            });
            if (!res.ok) {
                const errBody = await res.text();
                return {error: true, status: res.status, body: errBody.slice(0, 500)};
            }
            const body = await res.text();
            return {error: false, body: body};
        }
        """
        payload_json = json.dumps(payload, ensure_ascii=False)
        log.info("POST /chat/completion [chat_with_file, browser fetch]")
        result = await self._page.evaluate(
            js_code, [url, payload_json], isolated_context=False
        )

        if result.get("error"):
            raise RuntimeError(
                f"chat_with_file error {result.get('status')}: {result.get('body', '')[:200]}"
            )

        full_text = ""
        conv_id = None
        raw_body = result.get("body", "")
        for block in raw_body.split("\n"):
            line = block.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if not data_str or data_str == "{}":
                continue
            try:
                data = json.loads(data_str)
                full_text += self._extract_text(data)
                if not conv_id:
                    cid = self.extract_conversation_id(data)
                    if cid and cid != "0":
                        conv_id = cid
            except json.JSONDecodeError:
                continue

        return {"text": full_text, "conversation_id": conv_id}
