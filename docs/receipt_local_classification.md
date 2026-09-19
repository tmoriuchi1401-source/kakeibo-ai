# Local receipt kind recognition

Receipt intake now recognises sales structure in addition to literal words such
as 商品 or お買上げ. Parking slips require parking/time-or-receipt and payment
evidence. Itemised retail slips require tax, payment and subtotal/sale evidence;
dated dining slips may instead supply two item-price lines. Merchant names and
source IDs are not allowlists. A receipt heading or tax registration number
alone does not authorise submission.

New structural matches and undecided readable originals receive two bounded,
whole-page Japanese OCR passes (enlarged grayscale, then adaptive thresholding
with column layout). They use the already-installed local Tesseract model, not
an external AI or a newly downloaded model. All pages must complete (maximum
three PDF pages); each pass has a 25-second timeout. A failed/empty supplemental
pass cannot promote a blocked source. Original evidence is never discarded.

Sensitive terms are recognised despite OCR-inserted whitespace. Medical and
payroll evidence from any pass takes precedence; conflicting evidence stays
blocked. Positive sale structure must occur within one reading, rather than
being assembled from duplicated observations. Existing sensitive provenance and
the independent adapter-side exact-byte permission check remain enforced.

Supplemental OCR establishes document kind only. It does not supply payment
amounts or coordinates to medical posting. A newly recognised medical document
enters the existing medical review path with payment unconfirmed. An exact
version's untouched intake question can close automatically; owner input keeps
the question open. Classification itself does not add accounting entries.

Validation uses synthetic retail/parking/medical/payroll cases, split sensitive
terms, corrupt/partial PDF observations and owner edits. Production originals,
OCR text and identifiers are excluded from the repository and CI artifacts.

OCR design reference: [Tesseract image quality and segmentation](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html).
