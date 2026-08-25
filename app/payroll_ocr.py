from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class EncryptedPayrollPdfError(ValueError):
    pass


@dataclass(frozen=True)
class PositionedText:
    text: str
    page: int
    x: float
    y: float
    width: float
    height: float
    confidence: float


@dataclass(frozen=True)
class ExtractedPayrollText:
    text: str
    file_type: str
    extraction_method: str
    tokens: tuple[PositionedText, ...] = ()


def _ocr_image(image, page: int = 1) -> tuple[str, tuple[PositionedText, ...]]:
    import pytesseract
    from PIL import ImageEnhance, ImageOps

    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(1.7)
    # Small browser screenshots benefit from enlargement; this does not persist data.
    if image.width < 1800:
        image = image.resize((image.width * 2, image.height * 2))
    candidates = [pytesseract.image_to_string(image, lang="jpn+eng", config=f"--psm {psm}")
                  for psm in (4, 6)]
    data = pytesseract.image_to_data(image, lang="jpn+eng", config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    tokens = []
    for index, word in enumerate(data["text"]):
        word = word.strip()
        if not word: continue
        confidence = float(data["conf"][index])
        tokens.append(PositionedText(word, page, float(data["left"][index]),
                                     float(data["top"][index]), float(data["width"][index]),
                                     float(data["height"][index]), confidence))
    # Layout modes recover complementary columns; combining them is safer than
    # selecting one and the parser deduplicates repeated payroll items.
    return "\n".join(candidates), tuple(tokens)


def extract_payroll_text(path: str | Path, minimum_pdf_text: int = 80) -> ExtractedPayrollText:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            raise EncryptedPayrollPdfError("暗号化PDFは処理できません")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.strip()) >= minimum_pdf_text:
            tokens = []
            for page_number, pdf_page in enumerate(reader.pages, 1):
                page_height = float(pdf_page.mediabox.height)
                def visitor(value, cm, tm, font, font_size):
                    value = value.strip()
                    if not value: return
                    size = float(font_size or 10)
                    tokens.append(PositionedText(value, page_number, float(tm[4]),
                                                 page_height - float(tm[5]),
                                                 max(size, len(value) * size * .55), size, 100.0))
                pdf_page.extract_text(visitor_text=visitor)
            return ExtractedPayrollText(text, "pdf", "pdf_text", tuple(tokens))

        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        results = [_ocr_image(page.render(scale=3).to_pil(), index + 1)
                   for index, page in enumerate(document)]
        return ExtractedPayrollText("\n".join(result[0] for result in results), "pdf", "ocr",
                                    tuple(token for result in results for token in result[1]))

    from PIL import Image

    with Image.open(path) as image:
        text, tokens = _ocr_image(image)
        return ExtractedPayrollText(text, "image", "ocr", tokens)
