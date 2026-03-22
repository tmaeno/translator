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

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the package (pulls in all dependencies)
pip install -e .

# 3. Install Playwright's Chromium browser (one-time)
playwright install chromium
```

---

## Usage

```bash
# After pip install -e ., a console script is registered:
pdf-translator

# Or run directly from the repo:
source .venv/bin/activate
python main.py
```

1. Click **Open PDF…** and select your French PDF
2. The chapter list populates from the PDF's table of contents
3. Select one or more chapters to translate
4. Set an output path (defaults to `<original>_ja.pdf` next to the source file)
5. Click **Translate Selected Chapters**
6. A Chromium browser window opens — log in to your claude.ai account
7. Translation starts automatically once login is detected
8. When complete, a dialog shows the output path

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

- Long chapters are split into ~3000-character chunks to stay within Claude's input limits.
- A 2-second pause is inserted between chunks to avoid rate limiting.
- The output PDF uses a **reflow layout**: text flows naturally in Japanese and images are embedded at their relative positions. The page geometry does not match the original French layout exactly.
- Claude.ai's web UI may change over time. If the automator stops working, the CSS selectors in `src/claude_automator.py` may need to be updated.
