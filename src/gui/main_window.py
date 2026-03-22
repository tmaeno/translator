"""
Main PySide6 window for the PDF French→Japanese Translator.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

_SETTINGS_PATH = Path.home() / ".pdf-translator" / "settings.json"
_LOG_PATH = Path.home() / ".pdf-translator" / "translator.log"


def _load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(data))
    except Exception:
        pass

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QProgressBar,
    QTextEdit,
    QSplitter,
    QMessageBox,
    QFrame,
)

from src.pdf_parser import extract_chapters, extract_chapter_pages, extract_chapter_images, create_placeholder_pdf, Chapter
from src.claude_automator import ClaudeAutomator
from src.pdf_builder import HtmlPdfBuilder


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class TranslationWorker(QObject):
    log = Signal(str)
    progress = Signal(int)          # 0–100
    login_needed = Signal()         # emitted once browser is open
    login_ready = Signal()          # emitted once login detected
    finished = Signal(str)          # output path on success
    error = Signal(str)

    def __init__(
        self,
        pdf_path: str,
        chapters: list[Chapter],
        chapter_nums: list[int],
        output_path: str,
    ):
        super().__init__()
        self._pdf_path = pdf_path
        self._chapters = chapters
        self._chapter_nums = chapter_nums
        self._output_path = output_path
        self._cancelled = False

    def run(self):
        try:
            self._do_translation()
        except InterruptedError:
            self.error.emit("Translation cancelled.")
        except Exception as e:
            self.error.emit(str(e))

    def cancel(self):
        self._cancelled = True

    def _do_translation(self):
        total = len(self._chapters)

        def on_log(msg: str):
            self.log.emit(msg)

        def on_login_ready():
            self.login_ready.emit()

        with ClaudeAutomator(
            on_log=on_log,
            on_login_ready=on_login_ready,
            should_cancel=lambda: self._cancelled,
        ) as automator:
            self.login_needed.emit()
            automator.wait_for_login()

            chapter_pdfs: list[str] = []

            for idx, chapter in enumerate(self._chapters):
                if self._cancelled:
                    raise InterruptedError("Translation cancelled.")

                self.log.emit(f"\n=== Translating: {chapter.title} ===")

                # Step 1: extract chapter pages to a temp PDF
                tmp_pdf = Path(tempfile.mktemp(suffix=".pdf"))
                try:
                    self.log.emit("  Extracting chapter pages…")
                    extract_chapter_pages(self._pdf_path, chapter, tmp_pdf)

                    # Step 2: extract images and create placeholder PDF for Claude
                    self.log.emit("  Extracting images from original PDF…")
                    images = extract_chapter_images(self._pdf_path, chapter)
                    self.log.emit(f"  Found {len(images)} image(s).")

                    placeholder_pdf = create_placeholder_pdf(tmp_pdf, images, chapter.start_page)
                    # Step 3: upload placeholder PDF to Claude, get HTML
                    self.log.emit("  Uploading to Claude.ai…")
                    try:
                        html = automator.translate_pdf_to_html(placeholder_pdf, image_count=len(images))
                    finally:
                        placeholder_pdf.unlink(missing_ok=True)
                    # Save raw HTML for debugging (inspect [IMAGE_N] placement)
                    debug_path = Path.home() / ".pdf-translator" / f"debug_ch{self._chapter_nums[idx]}.html"
                    debug_path.write_text(html, encoding="utf-8")
                    self.log.emit(f"  Raw HTML saved to {debug_path}")
                finally:
                    tmp_pdf.unlink(missing_ok=True)

                if self._cancelled:
                    raise InterruptedError("Translation cancelled.")

                # Step 4: inject images + render to PDF
                ch_num = self._chapter_nums[idx]
                if total == 1:
                    out_path = self._output_path
                else:
                    # Multiple chapters → individual PDFs, merged at the end
                    stem = Path(self._pdf_path).stem
                    out_dir = Path(self._output_path).parent
                    out_path = str(out_dir / f"{stem}_ch{ch_num}_jp.pdf")

                builder = HtmlPdfBuilder(out_path, on_log=on_log)
                builder.build(html, images, automator.page)
                chapter_pdfs.append(out_path)

                self.progress.emit(int((idx + 1) / total * 100))

            # Step 5: merge chapter PDFs if more than one
            if total > 1:
                self.log.emit("\nMerging chapter PDFs…")
                _merge_pdfs(chapter_pdfs, self._output_path)
                # Clean up individual chapter PDFs
                for p in chapter_pdfs:
                    Path(p).unlink(missing_ok=True)

        self.finished.emit(self._output_path)


def _merge_pdfs(pdf_paths: list[str], output_path: str) -> None:
    import fitz
    merged = fitz.open()
    for p in pdf_paths:
        merged.insert_pdf(fitz.open(p))
    merged.save(output_path)
    merged.close()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Translator  FR → JA")
        self.setMinimumSize(700, 620)
        self.setAcceptDrops(True)

        self._pdf_path: str | None = None
        self._chapters: list[Chapter] = []
        self._worker: TranslationWorker | None = None
        self._thread: QThread | None = None

        self._build_ui()
        self._restore_last_file()

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()
                self._drop_zone.setStyleSheet(
                    "QLabel { border: 2px dashed #0078d7; border-radius: 6px;"
                    " background: #e8f0fe; color: #0078d7; }"
                )
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._reset_drop_zone_style()

    def dropEvent(self, event: QDropEvent):
        self._reset_drop_zone_style()
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self._load_pdf(path)
                break
        event.acceptProposedAction()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # --- PDF picker + drop zone ---
        self._drop_zone = QLabel("Drop a PDF here  —  or  —  click to browse")
        self._drop_zone.setAlignment(Qt.AlignCenter)
        self._drop_zone.setMinimumHeight(56)
        self._reset_drop_zone_style()
        self._drop_zone.mousePressEvent = lambda _: self._on_open_pdf()
        root.addWidget(self._drop_zone)

        self._pdf_label = QLabel("No file selected")
        self._pdf_label.setStyleSheet("color: grey;")
        root.addWidget(self._pdf_label)

        root.addWidget(_hline())

        # --- Chapter list ---
        root.addWidget(QLabel("Chapters (select one or more to translate):"))
        self._chapter_list = QListWidget()
        self._chapter_list.setSelectionMode(QListWidget.MultiSelection)
        self._chapter_list.setMinimumHeight(160)
        self._chapter_list.itemSelectionChanged.connect(self._update_output_from_selection)
        root.addWidget(self._chapter_list)

        root.addWidget(_hline())

        # --- Output ---
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output PDF:"))
        self._out_label = QLabel("(not set)")
        self._out_label.setStyleSheet("color: grey;")
        self._browse_out_btn = QPushButton("Browse…")
        self._browse_out_btn.clicked.connect(self._on_browse_output)
        out_row.addWidget(self._out_label, stretch=1)
        out_row.addWidget(self._browse_out_btn)
        root.addLayout(out_row)

        # --- Translate button + login continue ---
        btn_row = QHBoxLayout()
        self._translate_btn = QPushButton("Translate")
        self._translate_btn.setEnabled(False)
        self._translate_btn.setMinimumHeight(44)
        self._translate_btn.setStyleSheet(
            "font-size: 15px; font-weight: bold;"
            "background-color: #28a745; color: white; border-radius: 6px;"
            "padding: 4px 16px;"
        )
        self._translate_btn.clicked.connect(self._on_translate)
        self._continue_btn = QPushButton("Continue (after login)")
        self._continue_btn.setVisible(False)
        self._continue_btn.clicked.connect(self._on_continue_after_login)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.close)
        btn_row.addWidget(self._translate_btn, stretch=2)
        btn_row.addWidget(self._continue_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(quit_btn)
        root.addLayout(btn_row)

        root.addWidget(_hline())

        # --- Progress ---
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        # --- Status label ---
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        # --- Log ---
        root.addWidget(QLabel("Log:"))
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(130)
        root.addWidget(self._log_view, stretch=1)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF Files (*.pdf)"
        )
        if path:
            self._load_pdf(path)

    def _load_pdf(self, path: str):
        self._pdf_path = path
        self._pdf_label.setText(Path(path).name)
        self._pdf_label.setStyleSheet("")
        self._drop_zone.setText(Path(path).name)
        self._drop_zone.setStyleSheet(
            "QLabel { border: 2px solid #28a745; border-radius: 6px;"
            " background: #e8f5e9; color: #1a5c2e; font-weight: bold; padding: 8px; }"
        )

        self._log(f"Parsing chapters from {Path(path).name}…")
        try:
            self._chapters = extract_chapters(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not parse PDF:\n{e}")
            return

        self._chapter_list.clear()
        for ch in self._chapters:
            item = QListWidgetItem(
                f"{ch.title}  (pages {ch.start_page + 1}–{ch.end_page + 1})"
            )
            item.setData(Qt.UserRole, ch)
            self._chapter_list.addItem(item)

        self._update_output_from_selection()

        self._log(f"Found {len(self._chapters)} chapter(s).")
        self._update_translate_btn()
        _save_settings({**_load_settings(), "last_pdf": path})

    def _on_browse_output(self):
        default = self._out_label.text() if self._out_label.text() != "(not set)" else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save translated PDF as", default, "PDF Files (*.pdf)"
        )
        if path:
            if not path.endswith(".pdf"):
                path += ".pdf"
            self._out_label.setText(path)
            self._out_label.setStyleSheet("")
            self._update_translate_btn()

    def _on_translate(self):
        selected_items = self._chapter_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No chapters selected", "Please select at least one chapter.")
            return

        out_path = self._out_label.text()
        if not out_path or out_path == "(not set)":
            QMessageBox.warning(self, "No output path", "Please set an output PDF path.")
            return

        chapters = [item.data(Qt.UserRole) for item in selected_items]
        chapter_nums = [
            _chapter_num(item.data(Qt.UserRole).title, self._chapter_list.row(item) + 1)
            for item in selected_items
        ]

        self._translate_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._progress.setValue(0)
        self._status_label.setText("Opening browser… please log in to claude.ai.")
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"{'='*60}\n")
        except Exception:
            pass
        self._log("Starting translation worker…")

        self._worker = TranslationWorker(self._pdf_path, chapters, chapter_nums, out_path)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.login_needed.connect(self._on_login_needed)
        self._worker.login_ready.connect(self._on_login_ready_signal)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)

        self._thread.start()

    def _on_login_needed(self):
        self._status_label.setText(
            "A browser window has opened. Please log in to claude.ai, "
            "then click 'Continue (after login)' below."
        )
        self._continue_btn.setVisible(True)

    def _on_continue_after_login(self):
        self._continue_btn.setVisible(False)
        self._status_label.setText("Waiting for login detection…")

    def _on_login_ready_signal(self):
        self._continue_btn.setVisible(False)
        self._status_label.setText("Logged in. Translation in progress…")

    def _on_cancel(self):
        if self._worker:
            self._worker.cancel()
        self._cancel_btn.setVisible(False)
        self._status_label.setText("Cancelling…")

    def _on_finished(self, output_path: str):
        self._progress.setValue(100)
        self._log(f"\nTranslation complete. Output: {output_path}")
        self.close()

    def _on_error(self, msg: str):
        self._translate_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._continue_btn.setVisible(False)
        self._status_label.setText(f"Error: {msg}")
        self._log(f"ERROR: {msg}")
        QMessageBox.critical(self, "Error", msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _restore_last_file(self):
        last = _load_settings().get("last_pdf")
        if last and Path(last).is_file():
            self._load_pdf(last)

    def _reset_drop_zone_style(self):
        self._drop_zone.setStyleSheet(
            "QLabel { border: 2px dashed #aaa; border-radius: 6px;"
            " background: #f8f8f8; color: #555; padding: 8px; }"
        )

    def _update_output_from_selection(self):
        if not self._pdf_path:
            return
        selected = self._chapter_list.selectedItems()
        nums = ".".join(
            _chapter_num(i.data(Qt.UserRole).title, self._chapter_list.row(i) + 1)
            for i in selected
        ) if selected else "1"
        stem = Path(self._pdf_path).stem
        out = Path(self._pdf_path).parent / f"{stem}_ch{nums}_jp.pdf"
        self._out_label.setText(str(out))
        self._out_label.setStyleSheet("")
        self._update_translate_btn()

    def _update_translate_btn(self):
        ready = (
            self._pdf_path is not None
            and self._chapters
            and self._out_label.text() not in ("(not set)", "")
        )
        self._translate_btn.setEnabled(ready)

    def _log(self, msg: str):
        self._log_view.append(msg)
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass


def _chapter_num(title: str, fallback: int) -> str:
    """Extract a leading number from a chapter title, e.g. '3. Fonctions' → '3'."""
    m = re.match(r'(\d+)', title.strip())
    return m.group(1) if m else str(fallback)


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _run():
    """Console-script entry point installed by pip."""
    import sys
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Translator")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
