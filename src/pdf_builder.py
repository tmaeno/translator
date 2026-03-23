"""
HTML-based PDF builder.
Injects extracted images into Claude's translated HTML, then uses Playwright
to print the HTML to a PDF (MathJax handles math rendering in the browser).
"""
from __future__ import annotations

import base64
import re
import tempfile
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page

from src.pdf_parser import ImageBlock


class HtmlPdfBuilder:
    """
    Build a translated PDF from HTML + images.

    Parameters
    ----------
    output_path : destination PDF path
    on_log      : optional log callback
    """

    def __init__(self, output_path: str, on_log: Callable[[str], None] | None = None):
        self._output_path = output_path
        self._log = on_log or (lambda msg: print(msg))

    def build(self, html: str, images: list[ImageBlock], page: Page) -> None:
        """
        Inject *images* into *html* at [IMAGE_N] placeholders, then render
        to PDF via Playwright's page.pdf().
        """
        html = self._inject_images(html, images)
        self._render_to_pdf(html, page)

    # ------------------------------------------------------------------

    def _inject_images(self, html: str, images: list[ImageBlock]) -> str:
        """Replace [IMAGE_N] markers with inline base64 <img> tags."""
        for i, img_block in enumerate(images, start=1):
            b64 = base64.b64encode(img_block.image_bytes).decode()
            tag = (
                f'<figure style="text-align:center;margin:1.5em 0;">'
                f'<img src="data:image/png;base64,{b64}" '
                f'style="max-width:70%;height:auto;">'
                f'</figure>'
            )
            html = html.replace(f"[IMAGE_{i}]", tag)

        remaining = [f"[IMAGE_{i}]" for i in range(1, len(images) + 10)
                     if f"[IMAGE_{i}]" in html]
        if remaining:
            self._log(f"  Warning: unresolved image placeholders: {remaining}")

        return html

    # MathJax 3 config enabling $...$ and $$...$$ delimiters
    _MATHJAX_CONFIG = (
        '<script>\n'
        'MathJax = { tex: {\n'
        '  inlineMath: [["$","$"],["\\\\(","\\\\)"]],\n'
        '  displayMath: [["$$","$$"],["\\\\[","\\\\]"]]\n'
        '} };\n'
        '</script>\n'
    )
    _MATHJAX_CDN = (
        '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js">'
        '</script>'
    )
    _FONT_OVERRIDE = (
        '<style>\n'
        'body { font-family: "Hiragino Mincho ProN", "Noto Serif CJK JP", serif !important;'
        ' font-size: 11pt !important; }\n'
        'p, li, td, th { font-size: 1em !important; }\n'
        '</style>\n'
    )

    def _prepare_html(self, html: str) -> str:
        """Inject MathJax config and font override, regardless of what Claude generated."""
        # Inject MathJax config before the existing MathJax CDN script tag
        patched = re.sub(
            r'(<script[^>]*mathjax[^>]*>)',
            self._MATHJAX_CONFIG + r'\1',
            html, count=1, flags=re.IGNORECASE,
        )
        if patched == html:
            # MathJax was absent — inject config + CDN before </head>
            patched = html.replace(
                '</head>',
                self._MATHJAX_CONFIG + self._MATHJAX_CDN + '\n</head>',
                1,
            )
        # Inject font override before </head>
        patched = patched.replace('</head>', self._FONT_OVERRIDE + '</head>', 1)
        return patched

    def _render_to_pdf(self, html: str, page: Page) -> None:
        """Load HTML in the Playwright page, wait for MathJax, then print to PDF."""
        html = self._prepare_html(html)
        tmp = Path(tempfile.mktemp(suffix=".html"))
        try:
            tmp.write_text(html, encoding="utf-8")
            self._log("  Loading translated HTML in browser…")
            page.goto(f"file://{tmp}", wait_until="domcontentloaded")

            # Wait for MathJax to finish typesetting (if present)
            self._log("  Waiting for MathJax to render math…")
            try:
                page.wait_for_function(
                    "document.readyState === 'complete'",
                    timeout=15_000,
                )
                has_mathjax = page.evaluate("typeof MathJax !== 'undefined'")
                if has_mathjax:
                    page.evaluate(
                        "MathJax.typesetPromise ? MathJax.typesetPromise() : Promise.resolve()"
                    )
                    page.wait_for_timeout(3000)  # allow rendering to complete
            except Exception:
                pass  # MathJax not loaded or timed out — continue anyway

            self._log(f"  Saving PDF to {self._output_path}…")
            page.pdf(
                path=self._output_path,
                format="A4",
                print_background=True,
                margin={"top": "20mm", "bottom": "20mm",
                        "left": "20mm", "right": "20mm"},
            )
        finally:
            tmp.unlink(missing_ok=True)


