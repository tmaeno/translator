# PDF Translator — FR → JA

A desktop app that translates French PDF textbooks into Japanese, chapter by chapter.
Translation is powered by your **claude.ai subscription** via browser automation (no API key required).
Original images and graphs are preserved in the output PDF.

---

## Features

- Extracts chapters from the PDF's table of contents (or detects headings automatically)
- Lets you choose which chapter(s) to translate before starting
- Automates claude.ai in a real browser window using Playwright — uses your existing subscription
- Rebuilds the PDF with Japanese text (Hiragino font on macOS) and all original images inline
- PySide6 desktop GUI with progress bar and live log

---

## Requirements

- macOS (Hiragino Japanese font is used; other platforms need a CJK font installed)
- Python 3.11+
- A [claude.ai](https://claude.ai) account (Pro or free)

---

## Installation

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the package and all dependencies
pip install -e .

# 3. Install Playwright's Chrome browser (one-time)
playwright install chrome
```

---

## Usage

```bash
pdf-translator
```

1. Drop a PDF onto the window (or click to browse)
2. The chapter list populates from the PDF's table of contents
3. Select one or more chapters to translate
4. The output path defaults to `<original>_ch<N>_jp.pdf` next to the source file
5. Click **Translate**
6. A Chrome browser window opens — log in to your claude.ai account if prompted
7. Translation starts automatically once login is detected
8. The app closes automatically when complete

---

## Project structure

```
translator/
  main.py                  # Entry point
  requirements.txt
  src/
    pdf_parser.py          # Chapter extraction + content blocks (text/images)
    claude_automator.py    # Playwright automation of claude.ai
    pdf_builder.py         # Output PDF construction (reflow layout)
    gui/
      main_window.py       # PySide6 main window
```

---

## Notes

- The chapter PDF is uploaded to claude.ai and translated in one pass; each image region is replaced with a labeled placeholder before upload so Claude places them correctly in the HTML output.
- The output PDF is rendered from HTML via Playwright (MathJax handles LaTeX, Hiragino Mincho font is used for Japanese text on macOS).
- Claude.ai's web UI may change over time. If the automator stops working, the CSS selectors in `src/claude_automator.py` may need to be updated.
