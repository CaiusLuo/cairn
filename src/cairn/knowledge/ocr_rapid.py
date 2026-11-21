"""RapidOCR and pypdfium2 backend, loaded only for the optional OCR extra."""

from __future__ import annotations

import math
from typing import Any

# Leave the other cores to chat, API requests and the rest of ingestion.
OCR_THREADS = 2
# A PDF can declare a huge page; cap the bitmap so OCR cannot exhaust server RAM.
MAX_PAGE_PIXELS = 20_000_000


def render_scale(width: float, height: float, dpi: int) -> float:
    scale = dpi / 72
    area = width * height
    if area * scale * scale > MAX_PAGE_PIXELS:
        scale = math.sqrt(MAX_PAGE_PIXELS / area)
    return scale


class RapidOcrRecognizer:
    def __init__(self, data: bytes, *, dpi: int) -> None:
        import cv2
        import pypdfium2 as pdfium
        from rapidocr import RapidOCR

        cv2.setNumThreads(OCR_THREADS)
        self._pdf: Any = pdfium.PdfDocument(data)
        self._dpi = dpi
        self._engine: Any = RapidOCR(
            params={
                "EngineConfig.onnxruntime.intra_op_num_threads": OCR_THREADS,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "warning",
            }
        )

    def recognize(self, page_index: int) -> str:
        page = self._pdf[page_index]
        try:
            width, height = page.get_size()
            if width <= 0 or height <= 0:
                return ""
            bitmap = page.render(scale=render_scale(width, height, self._dpi))
            try:
                image = bitmap.to_numpy().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
        result = self._engine(image)
        texts = getattr(result, "txts", None) or ()
        return "\n".join(str(text) for text in texts if str(text).strip())

    def close(self) -> None:
        self._engine = None
        self._pdf.close()
