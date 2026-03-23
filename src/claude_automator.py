"""
Claude.ai browser automator using Playwright.
Runs in a QThread and emits Qt signals for progress/status updates.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright, BrowserContext, Page

CLAUDE_URL = "https://claude.ai"

# Persistent Chrome profile directory — login session is saved here so the
# user only needs to log in once.
PROFILE_DIR = Path.home() / ".pdf-translator" / "chrome-profile"

def _build_prompt(image_count: int) -> str:
    image_rule = (
        "- このPDFには [ IMAGE 1 ], [ IMAGE 2 ], ... と番号付きのプレースホルダーボックスが埋め込まれています。"
        "翻訳時に各ボックスが現れる位置に [IMAGE_1], [IMAGE_2], ... （アンダースコア形式）を入れてください。"
        "注意点：①図の内容は一切記述しない。"
        "②元のPDFでボックスが現れる位置（前後のテキストとの関係）をできるだけ忠実に再現する。\n"
    )
    if image_count > 0:
        image_rule += (
            f"- プレースホルダーボックスは正確に {image_count} 個あります。"
            f"[IMAGE_1] から [IMAGE_{image_count}] まで、すべて省略せず使ってください。\n"
        )
    return (
        "このPDFはフランス語の数学の教科書の一章です。日本語に翻訳してください。\n"
        "規則:\n"
        "- 数式はPDF内のLaTeX記法を一切変更せずそのまま出力してください。\n"
        + image_rule +
        "- 【重要】出力は必ずこのチャットメッセージ内の ```html\n...\n``` コードブロックとして直接書いてください。"
        "アーティファクト（プレビューパネル）は絶対に使用しないでください。"
        "アーティファクトを使うと自動処理側でHTMLを取得できないため、翻訳が失敗します。"
        "出力は完全なHTMLドキュメント（<!DOCTYPE html>から</html>まで）としてください。\n"
        "- 絶対に省略しないでください。「以下省略」「...」などは使わず、全文を翻訳してください。"
    )

# Selectors for the chat input
INPUT_SELECTORS = [
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"]',
    'textarea',
]


class ClaudeAutomator:
    """
    Manages a Playwright browser session against claude.ai.
    Use as a context manager to ensure cleanup.

    Parameters
    ----------
    on_log          : callback(str) — log messages sent to the GUI
    on_login_ready  : callback() — called once login is detected
    should_cancel   : callable() → bool — return True to abort immediately
    """

    def __init__(
        self,
        on_log: Callable[[str], None] | None = None,
        on_login_ready: Callable[[], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ):
        self._on_log = on_log or (lambda msg: print(msg))
        self._on_login_ready = on_login_ready or (lambda: None)
        self._should_cancel = should_cancel or (lambda: False)
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    @property
    def page(self) -> Page:
        return self._page

    def start(self):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._context.new_page()

        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        self._log("Opening claude.ai…")
        self._page.goto(f"{CLAUDE_URL}/new")

    def stop(self):
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login detection
    # ------------------------------------------------------------------

    def wait_for_login(self, poll_interval: float = 2.0):
        """Block until the chat input is visible (user has logged in)."""
        if self._is_chat_ready():
            self._log("Existing session detected — no login required.")
            self._on_login_ready()
            return
        self._log("Please log in to claude.ai in the browser window…")
        while True:
            if self._should_cancel():
                raise InterruptedError("Cancelled.")
            if self._is_chat_ready():
                self._log("Login detected.")
                self._on_login_ready()
                return
            time.sleep(poll_interval)

    def _is_chat_ready(self) -> bool:
        for selector in INPUT_SELECTORS:
            try:
                el = self._page.locator(selector).first
                if el.count() > 0 and el.is_visible():
                    return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # Translation: upload PDF → get HTML
    # ------------------------------------------------------------------

    def translate_pdf_to_html(self, pdf_path: Path, image_count: int = 0) -> str:
        """
        Upload *pdf_path* to a new Claude.ai conversation and return the
        translated HTML string.
        """
        if self._should_cancel():
            raise InterruptedError("Translation cancelled.")

        self._log("Starting new conversation…")
        self._page.goto(f"{CLAUDE_URL}/new")
        self._page.wait_for_load_state("networkidle")
        time.sleep(1)

        # -- Upload the PDF file --
        self._log("Uploading PDF to Claude…")
        self._upload_file(pdf_path)

        # -- Send prompt --
        self._log("Sending translation prompt…")
        input_el = self._find_element(INPUT_SELECTORS)
        if input_el is None:
            raise RuntimeError("Could not find Claude.ai chat input. Are you logged in?")

        prompt = _build_prompt(image_count)
        _clipboard_write(prompt)
        input_el.click()
        self._page.keyboard.press("Meta+A")
        self._page.keyboard.press("Meta+V")
        time.sleep(0.3)

        # Verify paste; fall back to execCommand
        if not input_el.inner_text().strip():
            self._log("  (clipboard paste didn't work, trying execCommand…)")
            self._page.evaluate(
                """(text) => {
                    const el = document.querySelector('div[contenteditable="true"]')
                            || document.querySelector('textarea');
                    if (!el) return;
                    el.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                }""",
                prompt,
            )
            time.sleep(0.3)

        # Try sending; retry up to 3 times if Claude doesn't start generating
        for attempt in range(3):
            input_el.press("Enter")
            time.sleep(0.5)
            _try_click_send_button(self._page)

            # Wait up to 20s for Claude to start generating (stop button appears)
            started = self._wait_for_generating_start(timeout=20.0)
            if started:
                break
            self._log(f"  Prompt may not have sent (attempt {attempt + 1}/3), retrying…")
            # Re-find input and re-paste before next attempt
            input_el = self._find_element(INPUT_SELECTORS)
            if input_el is None:
                raise RuntimeError("Chat input disappeared.")
            if not input_el.inner_text().strip():
                _clipboard_write(prompt)
                input_el.click()
                self._page.keyboard.press("Meta+A")
                self._page.keyboard.press("Meta+V")
                time.sleep(0.3)
        else:
            raise RuntimeError("Prompt was not sent after 3 attempts.")

        self._log("Waiting for Claude's translation (this may take several minutes)…")
        html = self._wait_for_html_response()
        self.delete_current_chat()
        return html

    def _upload_file(self, pdf_path: Path) -> None:
        """Attach a file to the current conversation via the file input element."""
        # Try to set files directly on the hidden input (Playwright bypasses visibility)
        try:
            file_input = self._page.locator('input[type="file"]').first
            if file_input.count() > 0:
                file_input.set_input_files(str(pdf_path))
                self._wait_for_upload_confirmation()
                return
        except Exception:
            pass

        # Fallback: click attachment button to reveal file input, then set files
        attach_selectors = [
            'button[aria-label*="ttach"]',
            'button[aria-label*="ile"]',
            'button[data-testid*="attach"]',
            'label[for*="file"]',
        ]
        for sel in attach_selectors:
            try:
                btn = self._page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    time.sleep(0.5)
                    file_input = self._page.locator('input[type="file"]').first
                    file_input.set_input_files(str(pdf_path))
                    self._wait_for_upload_confirmation()
                    return
            except Exception:
                pass

        raise RuntimeError("Could not find file upload input on Claude.ai.")

    def _wait_for_upload_confirmation(self, timeout: float = 60.0) -> None:
        """Wait until a file preview/chip appears, indicating the upload completed."""
        deadline = time.time() + timeout
        # Look for any element that resembles a file attachment chip
        chip_selectors = [
            '[data-testid*="file"]',
            '[class*="file-chip"]',
            '[class*="attachment"]',
            '[aria-label*=".pdf"]',
        ]
        while time.time() < deadline:
            for sel in chip_selectors:
                try:
                    el = self._page.locator(sel).first
                    if el.count() > 0:
                        return
                except Exception:
                    pass
            # Also accept: input box has content, or send button becomes enabled
            time.sleep(0.5)
        # Don't fail hard if we can't confirm — the upload may still have worked
        self._log("  (could not confirm upload; proceeding anyway)")

    def _wait_for_html_response(self, timeout: float = 600.0) -> str:
        """
        Poll the page until Claude finishes and an HTML document is extractable.

        Uses the stop/cancel button as the definitive "done" signal: when the
        button has been absent for 3 consecutive seconds, Claude has finished.
        Then we extract from the message code block or artifact (clipboard fallback).
        """
        deadline = time.time() + timeout
        done_since: float | None = None
        extract_attempts = 0

        time.sleep(3)  # give streaming a moment to start

        while time.time() < deadline:
            if self._should_cancel():
                raise InterruptedError("Translation cancelled.")

            # Fast path: HTML in a message code block (no artifact needed)
            text = self._extract_response_text()
            html = _extract_html_block(text)
            if html:
                self._log(f"  Response received from message ({len(html)} chars).")
                return html

            # Track when Claude stops generating (stop button gone)
            if self._is_still_generating():
                done_since = None
                extract_attempts = 0
            else:
                if done_since is None:
                    done_since = time.time()

            # Once "done" for 3+ seconds, attempt extraction
            if done_since and (time.time() - done_since) >= 3.0:
                extract_attempts += 1
                self._log(f"  Claude appears done — extracting (attempt {extract_attempts})…")
                text = self._extract_response_text()
                html = _extract_html_block(text)
                if html and len(html) > 500:
                    self._log(f"  Response received from message ({len(html)} chars).")
                    return html
                if extract_attempts >= 5:
                    raise RuntimeError(
                        "Claude finished but HTML could not be extracted after 5 attempts. "
                        "Check the browser window."
                    )
                # Reset and wait a bit more before retrying
                done_since = None
                time.sleep(3)
                continue

            time.sleep(1.5)

        # Timeout — last attempt
        html = _extract_html_block(self._extract_response_text())
        if html and len(html) > 500:
            self._log("  Warning: response may be incomplete (timeout).")
            return html
        raise TimeoutError("Timed out waiting for Claude.ai HTML response.")

    def _extract_response_text(self) -> str:
        """Extract text from the last assistant message (code block preferred)."""
        try:
            text = self._page.evaluate("""
                () => {
                    const msgSelectors = [
                        '[data-testid="assistant-message"]',
                        '.font-claude-message',
                        '[class*="claude-message"]',
                        '[class*="AssistantMessage"]',
                        '.prose',
                        '[class*="markdown"]',
                    ];
                    for (const sel of msgSelectors) {
                        const els = document.querySelectorAll(sel);
                        if (!els.length) continue;
                        const el = els[els.length - 1];
                        // Prefer rendered code block content (no backtick markers)
                        const codeEl = el.querySelector('pre > code, pre code');
                        if (codeEl) {
                            const c = codeEl.textContent || '';
                            if (c.trim().length > 50) return c;
                        }
                        const text = el.innerText;
                        if (text && text.trim()) return text;
                    }
                    return '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    def _wait_for_generating_start(self, timeout: float = 20.0) -> bool:
        """Return True once the stop button appears (Claude started generating)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._should_cancel():
                raise InterruptedError("Translation cancelled.")
            if self._is_still_generating():
                return True
            time.sleep(0.5)
        return False

    def _is_still_generating(self) -> bool:
        """Return True while Claude is actively streaming (stop button is visible)."""
        try:
            return bool(self._page.evaluate("""
                () => {
                    const sels = [
                        'button[aria-label*="Stop"]',
                        'button[data-testid*="stop"]',
                        'button[aria-label="Stop generating"]',
                    ];
                    for (const sel of sels) {
                        for (const el of document.querySelectorAll(sel)) {
                            if (el.offsetParent !== null) return true;
                        }
                    }
                    return false;
                }
            """))
        except Exception:
            return False


    def delete_current_chat(self) -> None:
        """Delete the current chat from Claude.ai's sidebar."""
        try:
            url = self._page.url
            chat_id = url.rstrip("/").split("/")[-1]
            if not chat_id or chat_id in ("new", "claude.ai"):
                return

            self._log("  Deleting chat from history…")

            chat_link = self._page.locator(f'a[href*="{chat_id}"]').first
            if chat_link.count() == 0:
                return
            chat_link.hover(force=True)
            time.sleep(0.3)

            menu_btn = self._page.locator(
                'button[aria-label*="more" i], button[aria-label*="option" i], '
                'button[data-testid*="more"], button[data-testid*="menu"]'
            ).last
            if menu_btn.count() == 0 or not menu_btn.is_visible():
                return
            menu_btn.click()
            time.sleep(0.3)

            delete_btn = self._page.locator(
                'button:has-text("Delete"), menuitem:has-text("Delete"), '
                '[role="menuitem"]:has-text("Delete")'
            ).first
            if delete_btn.count() == 0 or not delete_btn.is_visible():
                return
            delete_btn.click()
            time.sleep(0.3)

            confirm_btn = self._page.locator(
                'button:has-text("Delete"), button[data-testid*="confirm"]'
            ).last
            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                confirm_btn.click()

            time.sleep(0.5)
            self._log("  Chat deleted.")
        except Exception as e:
            self._log(f"  (Could not delete chat: {e})")

    def _find_element(self, selectors: list[str]):
        for sel in selectors:
            try:
                el = self._page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    return el
            except Exception:
                pass
        return None

    def _log(self, msg: str):
        self._on_log(msg)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _try_click_send_button(page: Page) -> None:
    """Click the send button if it is visible and enabled (fallback for Enter key)."""
    send_selectors = [
        'button[aria-label*="Send"]',
        'button[data-testid*="send"]',
        'button[type="submit"]',
    ]
    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                btn.click()
                return
        except Exception:
            pass


def _clipboard_write(text: str) -> None:
    """Write *text* to the macOS clipboard via pbcopy."""
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def _extract_html_block(text: str) -> str | None:
    """
    Extract an HTML document from *text*.

    Handles three cases:
    1. Raw HTML (returned from <code> element textContent — no fence markers)
    2. Fenced ```html ... ``` block (raw text fallback)
    3. Fenced ```html without closing ``` but with </html>
    """
    # Case 1: raw HTML content (no backtick markers) — the common case from Claude.ai
    m = re.search(r"(<!DOCTYPE\s+html[\s\S]+?</html>)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Case 2: ```html ... ``` fence
    m2 = re.search(r"```html\s*([\s\S]+?)```", text)
    if m2:
        return m2.group(1).strip()

    # Case 3: ```html with </html> but no closing ```
    m3 = re.search(r"```html\s*([\s\S]+</html>)", text)
    if m3:
        return m3.group(1).strip()

    return None
