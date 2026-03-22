"""
PDF parser: extracts chapter structure and images from a PDF.
"""
from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import fitz  # PyMuPDF


@dataclasses.dataclass
class Chapter:
    title: str
    start_page: int   # 0-based
    end_page: int     # 0-based, inclusive


@dataclasses.dataclass
class ImageBlock:
    image_bytes: bytes
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    page_index: int = 0   # absolute 0-based page index in the source PDF


def extract_chapters(pdf_path: str | Path) -> list[Chapter]:
    """Return chapters from the PDF's TOC, or fall back to heading detection."""
    doc = fitz.open(str(pdf_path))
    toc = doc.get_toc()  # [[level, title, page_1based], ...]

    chapters: list[Chapter] = []

    if toc:
        # Use only top-level entries (level == 1) as chapters
        top_level = [entry for entry in toc if entry[0] == 1]
        if not top_level:
            top_level = toc  # all levels if none at level 1

        for i, (_, title, page_1based) in enumerate(top_level):
            start = page_1based - 1  # convert to 0-based
            if i + 1 < len(top_level):
                end = top_level[i + 1][2] - 2  # page before next chapter
            else:
                end = doc.page_count - 1
            end = max(start, end)
            chapters.append(Chapter(title=title, start_page=start, end_page=end))
    else:
        # Fallback: scan for large-font short lines as headings
        chapters = _detect_chapters_by_headings(doc)

    doc.close()
    return chapters


def _detect_chapters_by_headings(doc: fitz.Document) -> list[Chapter]:
    """Heuristic: treat unusually large text on a new page as a chapter heading."""
    headings: list[tuple[int, str]] = []  # (page_idx, heading_text)

    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    size = span["size"]
                    text = span["text"].strip()
                    if size >= 14 and len(text) > 2 and len(text) < 80:
                        headings.append((page_idx, text))
                        break

    if not headings:
        # Treat entire PDF as one chapter
        return [Chapter(title="Document", start_page=0, end_page=doc.page_count - 1)]

    chapters: list[Chapter] = []
    for i, (page_idx, title) in enumerate(headings):
        end = headings[i + 1][0] - 1 if i + 1 < len(headings) else doc.page_count - 1
        chapters.append(Chapter(title=title, start_page=page_idx, end_page=max(page_idx, end)))
    return chapters


def extract_chapter_pages(pdf_path: str | Path, chapter: Chapter, out_path: str | Path) -> None:
    """Copy the chapter's pages into a new PDF at out_path."""
    src = fitz.open(str(pdf_path))
    dst = fitz.open()
    dst.insert_pdf(src, from_page=chapter.start_page, to_page=chapter.end_page)
    dst.save(str(out_path))
    src.close()
    dst.close()


def extract_chapter_images(pdf_path: str | Path, chapter: Chapter) -> list[ImageBlock]:
    """
    Extract images (raster + vector drawings) from the chapter's pages,
    sorted by vertical position (reading order).
    """
    doc = fitz.open(str(pdf_path))
    result: list[ImageBlock] = []

    for page_idx in range(chapter.start_page, chapter.end_page + 1):
        page = doc[page_idx]
        page_width = page.rect.width
        page_height = page.rect.height
        raw: list[tuple[float, ImageBlock]] = []

        # Raster images embedded in the PDF
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                img_data = doc.extract_image(xref)
                img_bytes = img_data["image"]
                img_bbox = page.get_image_bbox(img_info)
                if img_bbox:
                    bbox = (img_bbox.x0, img_bbox.y0, img_bbox.x1, img_bbox.y1)
                    raw.append((img_bbox.y0, ImageBlock(
                        image_bytes=img_bytes,
                        bbox=bbox,
                        page_width=page_width,
                        page_height=page_height,
                        page_index=page_idx,
                    )))
            except Exception:
                pass

        # Vector drawings (TikZ graphs, diagrams) rendered as PNG
        raw.extend(_extract_drawing_images(page, page_idx))

        raw.sort(key=lambda x: x[0])
        result.extend(block for _, block in raw)

    doc.close()
    return result


def _extract_drawing_images(page: fitz.Page, page_index: int = 0) -> list[tuple[float, ImageBlock]]:
    """
    Render regions of significant vector drawings as PNG images.
    LaTeX-typeset PDFs use TikZ/PGFPlots — these are PDF vector paths,
    not embedded raster images, so page.get_images() misses them.
    """
    drawings = page.get_drawings()
    if not drawings:
        return []

    rects = [
        d["rect"]
        for d in drawings
        if d["rect"].width > 30 and d["rect"].height > 30
    ]
    if not rects:
        return []

    rects.sort(key=lambda r: r.y0)
    clusters: list[list[fitz.Rect]] = []
    for r in rects:
        if clusters and r.y0 - clusters[-1][-1].y1 < 40:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    result: list[tuple[float, ImageBlock]] = []
    for cluster in clusters:
        x0 = min(r.x0 for r in cluster)
        y0 = min(r.y0 for r in cluster)
        x1 = max(r.x1 for r in cluster)
        y1 = max(r.y1 for r in cluster)

        w, h = x1 - x0, y1 - y0
        if w < 60 or h < 60:
            continue

        clip = fitz.Rect(
            max(0, x0 - 4), max(0, y0 - 4),
            min(page.rect.width, x1 + 4), min(page.rect.height, y1 + 4),
        )
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
        img_bytes = pix.tobytes("png")

        result.append((y0, ImageBlock(
            image_bytes=img_bytes,
            bbox=(clip.x0, clip.y0, clip.x1, clip.y1),
            page_width=page.rect.width,
            page_height=page.rect.height,
            page_index=page_index,
        )))

    return result


def create_placeholder_pdf(
    chapter_pdf: Path,
    images: list[ImageBlock],
    start_page_offset: int,
) -> Path:
    """
    Return a copy of *chapter_pdf* where each extracted image area is replaced
    with a clearly labelled [ IMAGE N ] box. Filtered-out images (grids) are
    left as-is — Claude is told the exact count N and looks only for labeled
    boxes, so unlabeled grids are ignored.
    """
    doc = fitz.open(str(chapter_pdf))
    for i, img in enumerate(images, start=1):
        page_idx = img.page_index - start_page_offset
        page = doc[page_idx]
        x0, y0, x1, y1 = img.bbox
        rect = fitz.Rect(x0, y0, x1, y1)
        page.draw_rect(rect, color=(0, 0, 0), fill=(1, 1, 1))          # white fill
        page.draw_rect(rect, color=(0.2, 0.2, 0.8), width=2)           # blue border
        font_size = max(8, min(14, (y1 - y0) * 0.5))
        page.insert_text(
            fitz.Point(x0 + 4, (y0 + y1) / 2 + font_size / 3),
            f"[ IMAGE {i} ]",
            fontsize=font_size,
            color=(0.2, 0.2, 0.8),
        )
    tmp = Path(tempfile.mktemp(suffix="_placeholder.pdf"))
    doc.save(str(tmp))
    doc.close()
    return tmp
