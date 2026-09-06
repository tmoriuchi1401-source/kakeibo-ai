# RapidOCR shadow adapter

This is an opt-in diagnostic component, not a production OCR selection or payment
resolver. No production caller imports it. RapidOCR, ONNX Runtime and model assets
are deliberately absent from production requirements and this repository.

## Boundary and data contract

`RapidOcrShadowAdapter.observe(ReceiptImage(...))` accepts exactly one immutable
PNG/JPEG image envelope. Mutable bytearray/memoryview inputs are rejected. A PDF
caller must use the existing bounded page renderer and pass each rendered page
independently. Animated/multiple-image containers and PDFs are rejected by the
worker. Unit identity and original page ordinal stay attached to each result;
there is no multi-unit merge or cache.

`OcrObservation` retains every detected text-region observation before RapidOCR's
blank/text-score output filters: raw text, four-point polygon, recognition score,
detection score, ordinal, engine/model provenance and source image digest.
Unequal detector/recognizer arrays retain orphan regions and mark incompleteness.
Blank, malformed, low-score and duplicate-value regions are not discarded.
Raw fields are repr-hidden and transient. Do not log, persist, call `asdict` on,
or publish these objects. The explicit projection summary contains only counts
and allowlisted diagnostic codes. A result is not an authorization for external AI.

Polygons describe OCR text regions, not cells or individual numeric substrings.
`project_observation` creates existing `PaymentRegionEvidence` / `NumericObservation`
concepts while retaining the neutral source DTO and NFKC character offsets.
It never fabricates Tesseract word boxes, rescales confidence to Tesseract's scale,
repairs digits, pairs separate regions or creates payment candidates. A 0.7 score
band is descriptive/un-calibrated; it does not confer reliability or confirmation.
Malformed evidence survives even when a low score determines the numeric state.
Raw polygons remain on the source; numeric substring bbox is explicitly absent.
Text and structured views are not counted as independent signals. Every projected
region has the same source observation group, with its original unit/page identity
on the enclosing DTO. Consume the projection within that unit only.

The common evidence resolver can inspect this projection but receives zero
candidates. The adapter and its summary always remain in the needs_review domain.
Strong/weak/negative context means existing lexical categories, not proof of total
payment role. Observations from unrelated regions remain unresolved observations.

## Offline worker and model pinning

Supply an absolute path to a trusted, pre-provisioned optional Python environment,
and explicit absolute local paths plus SHA256 for detector/classifier/recognizer.
No installation, URL input, model search, automatic download or font rendering is
performed. Missing assets, hash mismatch and unsupported versions fail closed.
Supported audited combination:

- RapidOCR 3.9.2 and ONNX Runtime 1.29.0.
- PP-OCRv6_det_small.onnx, PP-OCRv6_rec_small.onnx and
  ch_ppocr_mobile_v2.0_cls_mobile.onnx. Exact SHA256 values are in
  `SUPPORTED_ASSETS`; other model hashes are rejected, even if supplied by caller.
- CPU only, four intra-op threads / one inter-op thread, standard OCR thresholds.

Each call starts an isolated-interpreter, short-lived worker. The worker verifies
the actual installed versions and model hashes, then creates ONNX sessions from
the exact verified immutable model bytes. Model files are never reopened for
inference, avoiding check/load races. It requires the embedded recognition
dictionary and CPUExecutionProvider only. The RapidOCR-specific session and
pre-filter hooks are isolated in `medical_rapidocr_worker.py` and version-gated.
An upstream error, incompatible shape, malformed reply or timeout returns a
redacted incomplete observation rather than an empty successful OCR result.

Binary image input and raw OCR response travel only over private parent/child
pipes. They are not CLI output or logs. Worker terminal use is refused; ordinary
Python and native stdout/stderr are suppressed. No image/OCR output file is made.
Image size, header/reply size and execution time are bounded. The trusted optional
environment must not be supplied by an untrusted receipt/upload caller.

ORT_DISABLE_TELEMETRY is set before ONNX Runtime import and telemetry is disabled
before session creation. Python socket/DNS entry points, requests and RapidOCR's
asset downloader are denied for the worker lifetime. Even a swallowed attempted
network operation vetoes success. Production process sockets are never patched.
These code guards are not an OS-level network sandbox for native libraries.
Before production deployment, enforce outbound denial on the worker and repeat
the native dependency/telemetry audit on the actual production OS. This shadow
phase does not alter the existing application privacy boundary.

## Commercial distribution and upgrades

RapidOCR is Apache-2.0; the selected upstream model cards and RapidAI model
distribution declare Apache-2.0. Retain applicable copyright, LICENSE and NOTICE
files and identify modifications when distributing. ONNX Runtime is MIT.
Dependency distribution has additional obligations: Shapely includes LGPL GEOS,
OpenCV wheels include third-party components such as LGPL FFmpeg, and some Python
dependencies carry MPL terms. Inventory the exact target wheels/native binaries;
provide notices and applicable source/replacement/relinking provisions for the
actual distribution form. Internal server use and customer binary redistribution
need separate release checks. No font is downloaded or redistributed here.

Official references:

- https://pypi.org/project/rapidocr/
- https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det
- https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec
- https://www.modelscope.cn/models/RapidAI/RapidOCR/tree/master/onnx/PP-OCRv4/cls
- https://www.apache.org/licenses/LICENSE-2.0.html
- https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md
- https://shapely.readthedocs.io/en/stable/
- https://github.com/opencv/opencv-python/blob/4.x/README.md

Provision public artifacts separately from medical inference. Lock every wheel
and model hash, including transitive/native dependencies; preserve license files
with the distribution. The evaluated Windows/Python 3.14 environment used
OmegaConf 2.3.1 with an isolated build of ANTLR runtime 4.9.3; avoid resolver
backtracking to an older OmegaConf merely to get binary-only installation.
Approximate audited OCR closure: 109 MB compressed / 285 MB installed, including
32 MB models. Python, PDF/Tesseract tooling and inference RAM are additional.

For an upgrade, review official model licenses, telemetry/download paths, native
wheel contents and RapidOCR pre-filter/session API first. Rebuild a hash-locked
optional environment, update the explicit supported combination only after that
review, run synthetic contract/offline/negative-evidence tests, then evaluate
independent receipt units without reference-based tuning. Run relevant and full
production tests before checkpointing. Do not auto-upgrade a deployed model.
Windows CPU was evaluated; other OS deployments require their own verification.
