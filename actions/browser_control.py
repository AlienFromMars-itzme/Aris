import asyncio
import concurrent.futures
import json
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote_plus

import zendriver as zd


def _get_default_browser_id() -> str:
    """Returns raw default browser identifier string for current OS."""
    system = platform.system()
    try:
        if system == "Windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
            )
            prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
            winreg.CloseKey(key)
            return prog_id

        elif system == "Darwin":
            result = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

        elif system == "Linux":
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

    except Exception:
        pass

    return ""


_BROWSER_BINARIES = {
    "Windows": {
        "opera":   ["opera.exe"],
        "brave":   ["brave.exe"],
        "vivaldi": ["vivaldi.exe"],
        "edge":    ["msedge.exe"],
        "chrome":  ["chrome.exe"],
        "chromium": ["chromium.exe"],
    },
    "Darwin": {
        "opera":   ["opera"],
        "brave":   ["brave browser", "brave"],
        "vivaldi": ["vivaldi"],
        "edge":    ["microsoft edge", "msedge"],
        "chrome":  ["google chrome", "google-chrome"],
        "chromium": ["chromium"],
    },
    "Linux": {
        "opera":   ["opera", "opera-stable"],
        "brave":   ["brave-browser", "brave"],
        "vivaldi": ["vivaldi-stable", "vivaldi"],
        "edge":    ["microsoft-edge", "microsoft-edge-stable", "msedge"],
        "chrome":  ["google-chrome", "google-chrome-stable"],
        "chromium": ["chromium-browser", "chromium"],
    },
}


def _get_opera_executable() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        candidate_keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\launcher.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
        ]
        for key_path in candidate_keys:
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(key, None)
                    winreg.CloseKey(key)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] 🔍 Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _find_browser_executable(prog_id: str) -> tuple[str | None, bool]:
    """
    Returns (exe_path, is_opera) for Chromium-family browsers supported by Zendriver.
    """
    system = platform.system()
    os_bins = _BROWSER_BINARIES.get(system, {})

    if any(x in prog_id for x in ["firefox", "mozilla", "safari"]):
        print("[Browser] ⚠️ Zendriver uses Chromium/CDP; ignoring non-Chromium default browser")

    if "opera" in prog_id:
        exe = _get_opera_executable()
        if exe:
            return exe, True
        for binary in os_bins.get("opera", []):
            path = shutil.which(binary)
            if path:
                return path, True

    browser_patterns = {
        "brave":   ["brave"],
        "vivaldi": ["vivaldi"],
        "edge":    ["edge", "msedge"],
        "chrome":  ["chrome"],
        "chromium": ["chromium"],
    }
    ordered_browsers = list(browser_patterns)
    matched_browsers = [
        browser_name
        for browser_name, patterns in browser_patterns.items()
        if any(p in prog_id for p in patterns)
    ]
    for browser_name in matched_browsers + ordered_browsers:
        for binary in os_bins.get(browser_name, []):
            path = shutil.which(binary)
            if path:
                print(f"[Browser] 🔍 Found {browser_name} at: {path}")
                return path, False

    return None, False


class _BrowserThread:

    def __init__(self):
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._browser = None
        self._page = None
        self._exe_path = None
        self._is_opera = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="BrowserThread"
        )
        self._thread.start()
        self._ready.wait(timeout=15)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro, timeout: int = 30):
        if not self._loop:
            raise RuntimeError("BrowserThread not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Tarayıcı ve sayfa yönetimi ───────────────────────────────────────────

    async def _launch_browser_if_needed(self):
        """
        Tarayıcıyı başlatır. Zaten açıksa hiçbir şey yapmaz.
        Zendriver Chromium/CDP tabanlıdır, bu yüzden Chromium ailesi tarayıcıları kullanır.
        """
        if self._browser and not getattr(self._browser, "stopped", False):
            return

        prog_id = _get_default_browser_id()
        self._exe_path, self._is_opera = _find_browser_executable(prog_id)

        browser_args = ["--start-maximized"]
        if self._is_opera:
            # Opera GX bazı sürümlerde varsayılan olarak private modda başlar.
            # Aşağıdaki flag'ler bunu engeller.
            browser_args += [
                "--disable-features=OperaPrivacyMode",
                "--no-private",
            ]
            print("[Browser] 🎭 Opera detected — disabling private-mode flags")

        launch_kwargs = {
            "headless": False,
            "browser_args": browser_args,
        }
        if self._exe_path:
            launch_kwargs["browser_executable_path"] = self._exe_path

        try:
            self._browser = await zd.start(**launch_kwargs)
            self._page = None
            print(
                "[Browser] ✅ Launched with Zendriver"
                f"{' / ' + self._exe_path if self._exe_path else ''}"
            )
        except Exception as e:
            if not self._exe_path:
                raise
            print(f"[Browser] ⚠️ Launch failed ({e}), falling back to Zendriver auto browser")
            self._browser = await zd.start(headless=False, browser_args=["--start-maximized"])
            self._page = None

    async def _get_page(self):
        """
        Mevcut sayfayı döndürür.
        - Tarayıcı kapalıysa açar.
        - Sayfa yoksa yeni sekme açar.
        - Sayfa zaten açıksa aynı sayfayı döndürür.
        """
        await self._launch_browser_if_needed()

        if self._page is None:
            self._page = await self._browser.get("about:blank")

        return self._page

    @staticmethod
    def _js(value) -> str:
        return json.dumps(value)

    @staticmethod
    def _text_match_script(text: str, action: str = "click") -> str:
        text_json = json.dumps(text.lower())
        action_json = json.dumps(action)
        return f"""
(() => {{
  const needle = {text_json};
  const action = {action_json};
  const candidates = Array.from(document.querySelectorAll('button, a, input, textarea, select, [role], [placeholder], [aria-label], label, *'));
  const visible = (el) => {{
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  }};
  const labelFor = (el) => {{
    if (!el.id) return '';
    const label = document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`);
    return label ? label.innerText : '';
  }};
  const score = (el) => {{
    const haystack = [el.innerText, el.textContent, el.value, el.placeholder, el.ariaLabel, el.getAttribute('aria-label'), el.getAttribute('role'), labelFor(el)]
      .filter(Boolean).join(' ').toLowerCase();
    if (!haystack.includes(needle)) return -1;
    return Math.abs(haystack.length - needle.length);
  }};
  const matches = candidates.filter(visible).map(el => [el, score(el)]).filter(([, s]) => s >= 0).sort((a, b) => a[1] - b[1]);
  const el = matches[0]?.[0];
  if (!el) return false;
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  if (action === 'click') el.click();
  else el.focus();
  return true;
}})()
"""

    @staticmethod
    def _role_selector(role: str) -> str:
        role_map = {
            "button": "button, input[type='button'], input[type='submit'], input[type='reset'], [role='button']",
            "link": "a[href], [role='link']",
            "searchbox": "input[type='search'], [role='searchbox']",
            "textbox": "input:not([type]), input[type='text'], input[type='email'], input[type='password'], input[type='search'], textarea, [contenteditable='true'], [role='textbox']",
        }
        return role_map.get(role, f"[role={json.dumps(role)}]")

    # ── Eylemler ─────────────────────────────────────────────────────────────

    async def _go_to(self, url: str) -> str:
        if not url.startswith("http"):
            url = "https://" + url
        try:
            await self._launch_browser_if_needed()
            self._page = await self._browser.get(url)
            await self._page.wait_for_ready_state("interactive", timeout=15)
            page_url = getattr(self._page, "url", url)
            return f"Opened: {page_url}"
        except asyncio.TimeoutError:
            return f"Timeout loading: {url}"
        except Exception as e:
            return f"Navigation error: {e}"

    async def _search(self, query: str, engine: str = "google") -> str:
        encoded_query = quote_plus(query)
        engines = {
            "google":     f"https://www.google.com/search?q={encoded_query}",
            "bing":       f"https://www.bing.com/search?q={encoded_query}",
            "duckduckgo": f"https://duckduckgo.com/?q={encoded_query}",
        }
        url = engines.get(engine.lower(), engines["google"])
        return await self._go_to(url)

    async def _click(self, selector=None, text=None) -> str:
        page = await self._get_page()
        try:
            if text:
                clicked = await page.evaluate(self._text_match_script(text, "click"))
                return f"Clicked: '{text}'" if clicked else "Element not found or not clickable."
            elif selector:
                element = await page.select(selector, timeout=8)
                await element.click()
                return f"Clicked: {selector}"
            return "No selector or text provided."
        except asyncio.TimeoutError:
            return "Element not found or not clickable."
        except Exception as e:
            return f"Click error: {e}"

    async def _type(self, selector=None, text: str = "", clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            if selector:
                element = await page.select(selector, timeout=8)
                if clear_first:
                    await element.clear_input()
                await element.send_keys(text)
            else:
                await page.evaluate(f"""
(() => {{
  const el = document.activeElement;
  if (!el) return false;
  const text = {self._js(text)};
  const clearFirst = {str(bool(clear_first)).lower()};
  if ('value' in el) el.value = clearFirst ? text : el.value + text;
  else el.textContent = clearFirst ? text : el.textContent + text;
  el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: text}}));
  el.dispatchEvent(new Event('change', {{bubbles: true}}));
  return true;
}})()
""")
            return "Text typed."
        except Exception as e:
            return f"Type error: {e}"

    async def _scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.evaluate(f"window.scrollBy(0, {int(y)})")
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def _press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.evaluate(f"""
(() => {{
  const key = {self._js(key)};
  const el = document.activeElement || document.body;
  for (const type of ['keydown', 'keyup']) {{
    el.dispatchEvent(new KeyboardEvent(type, {{key, bubbles: true, cancelable: true}}));
  }}
  return true;
}})()
""")
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def _get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            text = text or ""
            return text[:4000] if len(text) > 4000 else text
        except Exception as e:
            return f"Could not get page text: {e}"

    async def _fill_form(self, fields: dict) -> str:
        page = await self._get_page()
        results = []
        for selector, value in fields.items():
            try:
                el = await page.select(selector, timeout=8)
                await el.clear_input()
                await el.send_keys(str(value))
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def _smart_click(self, description: str) -> str:
        page = await self._get_page()
        desc_lower = description.lower()

        role_hints = {
            "button":    ["button", "buton", "btn"],
            "link":      ["link", "bağlantı"],
            "searchbox": ["search", "arama"],
            "textbox":   ["input", "field", "alan"],
        }
        for role, keywords in role_hints.items():
            if any(k in desc_lower for k in keywords):
                try:
                    selector = self._role_selector(role)
                    element = await page.select(selector, timeout=5)
                    await element.click()
                    return f"Clicked ({role}): '{description}'"
                except Exception:
                    pass

        try:
            clicked = await page.evaluate(self._text_match_script(description, "click"))
            if clicked:
                return f"Clicked (text): '{description}'"
        except Exception:
            pass

        return f"Could not find: '{description}'"

    async def _smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()

        selectors = [
            f"[placeholder*={self._js(description)} i]",
            f"[aria-label*={self._js(description)} i]",
            "input:not([type]), input[type='text'], input[type='email'], input[type='password'], input[type='search'], textarea, [contenteditable='true'], [role='textbox']",
        ]
        for method, selector in [
            ("placeholder", selectors[0]),
            ("label", selectors[1]),
            ("role", selectors[2]),
        ]:
            try:
                el = await page.select(selector, timeout=5)
                await el.clear_input()
                await el.send_keys(text)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue

        try:
            focused = await page.evaluate(self._text_match_script(description, "focus"))
            if focused:
                return await self._type(None, text, True)
        except Exception:
            pass

        return f"Could not find input: '{description}'"

    async def _close_browser(self) -> str:
        if self._browser:
            await self._browser.stop()
            self._browser = None
            self._page = None

        return "Browser closed."


# ── Singleton browser thread ─────────────────────────────────────────────────

_bt = _BrowserThread()
_bt_started = False
_bt_lock = threading.Lock()


def _ensure_started():
    global _bt_started
    with _bt_lock:
        if not _bt_started:
            _bt.start()
            _bt_started = True


# ── Public API ───────────────────────────────────────────────────────────────

def browser_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    """
    Browser controller — auto-detects and uses a Chromium-family browser through Zendriver.
    Always reuses the existing browser window/page; never opens incognito.

    parameters:
        action      : go_to | search | click | type | scroll | fill_form |
                      smart_click | smart_type | get_text | press | close
        url         : URL for go_to
        query       : search query
        engine      : google | bing | duckduckgo (default: google)
        selector    : CSS selector for click/type
        text        : text to click or type
        description : element description for smart_click/smart_type
        direction   : up | down for scroll
        amount      : scroll amount in pixels (default: 500)
        key         : key name for press (e.g. Enter, Escape, Tab)
        fields      : {selector: value} dict for fill_form
        clear_first : bool, clear input before typing (default: True)
    """
    _ensure_started()

    action = (parameters or {}).get("action", "").lower().strip()
    result = "Unknown action."

    try:
        if action == "go_to":
            result = _bt.run(_bt._go_to(parameters.get("url", "")))

        elif action == "search":
            result = _bt.run(_bt._search(
                parameters.get("query", ""),
                parameters.get("engine", "google"),
            ))

        elif action == "click":
            result = _bt.run(_bt._click(
                selector=parameters.get("selector"),
                text=parameters.get("text"),
            ))

        elif action == "type":
            result = _bt.run(_bt._type(
                selector=parameters.get("selector"),
                text=parameters.get("text", ""),
                clear_first=parameters.get("clear_first", True),
            ))

        elif action == "scroll":
            result = _bt.run(_bt._scroll(
                direction=parameters.get("direction", "down"),
                amount=parameters.get("amount", 500),
            ))

        elif action == "fill_form":
            result = _bt.run(_bt._fill_form(parameters.get("fields", {})))

        elif action == "smart_click":
            result = _bt.run(_bt._smart_click(parameters.get("description", "")))

        elif action == "smart_type":
            result = _bt.run(_bt._smart_type(
                parameters.get("description", ""),
                parameters.get("text", ""),
            ))

        elif action == "get_text":
            result = _bt.run(_bt._get_text())

        elif action == "press":
            result = _bt.run(_bt._press(parameters.get("key", "Enter")))

        elif action == "close":
            result = _bt.run(_bt._close_browser())

        else:
            result = f"Unknown action: {action}"

    except concurrent.futures.TimeoutError:
        result = "Browser action timed out."
    except Exception as e:
        result = f"Browser error: {e}"

    print(f"[Browser] {result[:80]}")
    if player:
        player.write_log(f"[browser] {result[:60]}")

    return result
