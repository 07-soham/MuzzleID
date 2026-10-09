# MuzzleID system report

**Status:** executable MVP foundation; not a complete or validated biometric identification system.

## Product and implementation

The repository contained only raw dataset directories at the start of work. The current implementation adds a dataset audit, model/loss definitions, image-quality validation, a FastAPI metadata service and an operator portal served from that API. Registrations persist animal metadata and uploads with `pending_model` status. Verification returns HTTP 503 until the full model pipeline and calibrated thresholds are available; no match, score, or decision is fabricated.

## Dataset audit

Audit report: [`dataset_report.md`](dataset_report.md). The detailed `dataset_report.json` is generated locally and excluded from Git because it includes image-level paths and identity labels. Raw image data was not changed.

| Partition | Image files | Labeled images | Unique file contents | Identities | Corrupt/unreadable |
|---|---:|---:|---:|---:|---:|
| Train | 4,943 | 4,943 | 4,923 | 268 | 0 |
| Test / Modified | 1,486 | 743 | 743 | 24 | 0 |
| Test / Original | 1,486 | 743 | 743 | 24 | 0 |

All image files are JPEG. Test partition `*Master` folders duplicate the files in class directories and are not identities. Train has 4–70 images per identity (mean 18.444); each labeled test identity has 19–43 images (mean 30.958). Train has 20 exact duplicate groups; each test mirror has 743 exact-content groups between the flat master folder and labeled images. Same 8×8 average-hash groups are candidate collisions, not confirmed semantic duplicates. The audit found no separately recognized annotation data and inferred no boxes.

Literal folder labels do not overlap between train (`cattle_*`) and test (numeric labels), but this does **not** establish identity-level separation because the label namespaces differ. Provenance mapping is needed before claiming no leakage. Test files were not used for training or calibration.

## Model and metrics

- **Defined architecture:** ImageNet-weighted ResNet-50 backbone, linear 512-dimensional embedding head and L2 normalization.
- **Defined objective:** batch-hard triplet loss, requiring multiple examples per identity in a batch.
- **Training configuration and completed training:** Not available. Training, evaluation, validation calibration, threshold freeze and final test stages are not implemented or run.
- **Validation/test accuracy, FAR, FRR, EER, open-set performance, calibrated thresholds:** Not available.
- **Weights, YOLO detector, FAISS index:** Not available.

The current Python 3.13 runtime has no PyTorch, torchvision or FAISS installation. The model code was syntax-checked but not instantiated or trained.

## Service and data model

FastAPI serves the portal and OpenAPI. SQLAlchemy defines `animals`, `animal_images`, `registration_requests` (idempotency keys) and `verifications`. Local mode defaults to SQLite; Compose configures PostgreSQL. Animal images are written using generated UUID filenames after size and image-decoding validation. API routes include health, searchable animal listing, animal details/registration, verification and history. Verification history remains empty until an actual verification pipeline exists.

The browser UI is static HTML/CSS/JavaScript. It provides a dashboard, registry, registration form, verification form, history area and roadmap labeling future workflows as planned. It calls the API directly. It is not a React/Next.js build.

## Security and limitations

Uploads are size-limited, decoded with Pillow and stored using generated names; raw client paths are not used. Detailed internal exceptions are logged server-side and a generic save error is returned. There is no authentication, authorization, organization isolation, database migration framework, malware scanning, rate limiting, retention policy, YOLO localization, embedding gallery, insurance workflow, alert review, analytics, or API integration. Do not use this MVP for consequential insurance or ownership decisions.

## Commands and verification performed

- `python -m src.data.validation` — completed; generated dataset reports.
- `python -m pytest -q -p no:cacheprovider tests` — **6 passed**.
- `python -m compileall -q src tests` — passed.
- `node --check web\app.js` — passed.
- Dataset-image registration integration — two reference images saved, idempotent retry reused the same record, and a fresh process read the persisted record.
- Browser route checks covered all available and planned views; search/detail navigation worked, browser console had no remaining errors, and visual checks covered desktop/tablet/mobile.
- Docker CLI was available, but `docker info` failed because the Docker Desktop engine pipe was unavailable; Docker build/Compose startup were not run.
- No training, model evaluation, calibration, or successful biometric identification ran. A frontend production build is not applicable because there is no frontend package/build configuration.

## Launch

From PowerShell in the repository root, with Python 3.10/3.11:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-mvp.txt
$env:DATABASE_URL = "sqlite:///./muzzleid.db"
$env:UPLOAD_DIR = "./storage/uploads"
python -m uvicorn src.api.main:app --reload
```

Open `http://127.0.0.1:8000`; API documentation is at `/docs`. Animal registration works; biometric verification correctly reports unavailable until model artifacts, gallery and validated thresholds are provided.

## UI polish and functional regression results — 2026-10-09

### Root causes found

- Registration previously wrote metadata and uploaded files only. It never created embeddings or updated a vector index; animal records were therefore marked `pending_model`.
- Verification intentionally returned 503 because no trained checkpoint, YOLO model, FAISS index, or calibrated thresholds exist in this workspace. The missing artifacts are the blocker; generating scores would have been misleading.
- The portal originally had no animal-detail endpoint, displayed the same hard-coded status for every row, allowed repeated submits, and provided only anchor-like navigation. A planned-page routing bug hid its planned-state screen. The settings module also did not load `.env` even though the README instructed operators to configure it there.
- The default Python launcher initially failed to start in the sandbox. Tests ran successfully with the installed Python runtime using the allowed elevated execution.

### Changes made

- Added API-backed registry search, sorting, pagination and animal detail retrieval. The health route now checks the database connection and reports model/detector/index/threshold availability independently. Settings now load `.env` without overriding explicitly set process environment variables.
- Added idempotency keys for registration retries, duplicate-image rejection within one request, temporary-file cleanup on failure, and an atomic rename before saving files. The UI disables duplicate submits, preserves inputs after failures, and only reports persistence after the API confirms it.
- Added one shared API client, separate hash-routed portal views, database-backed details/history, searchable registry controls, accessible row navigation, image previews/removal/drop zone, validation feedback, responsive refinements, explicit planned views, and a local favicon.

### Verification

- `python -m pytest -q -p no:cacheprovider tests` — **6 passed**.
- `python -m compileall -q src tests`, `node --check web\api.js`, and `node --check web\app.js` — passed.
- Dataset-image registration integration: FastAPI returned 200; two images were persisted and visible in the registry/detail response. Retrying the same `Idempotency-Key` returned the same animal and did not create another record or file.
- Fresh Python process against the isolated SQLite database found the registered record after restart.
- Invalid verification image returned 422. A valid image submitted to verification returned 503 because model weights and calibrated thresholds are absent. Same-animal and unknown-animal biometric decisions were therefore **not testable**.
- Browser portal smoke test covered landing, dashboard, registry, registration, verification, history, system status, and the planned insurance/alerts/analytics/reports/settings pages. Registry search and animal-detail navigation worked. A real dataset registration through the browser showed two image previews, persisted successfully, and displayed the animal detail with two stored image records.
- Verification form submitted another real image and displayed the explicit unavailable-model response. Browser console showed **no errors**; requests returned expected 200/503 statuses. An initial planned-route bug was fixed and routes were retested.
- Responsive visual checks were performed at desktop, 768 px tablet and 390 px mobile sizes. The HTML portal has no frontend package, TypeScript, lint, or production build configuration, so those build checks do not apply.
- Docker CLI exists, but the Docker Desktop engine is unavailable; image build and Compose startup remain unverified.

### Remaining blockers

No trained model, detector, FAISS implementation/index, calibration artifacts, authentication/authorization, schema migration framework, insurance/alert/analytics backend, or true React/TypeScript frontend exists. Database/FAISS consistency is **not established** because embeddings and a FAISS index are not implemented. The application remains an MVP prototype and must not be used for consequential identity or insurance decisions.
