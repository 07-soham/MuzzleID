# MuzzleID

MuzzleID is a secured single-operator prototype for cattle records and muzzle-image identity research. It includes a FastAPI portal, encrypted reference-image storage, metric-learning training, versioned image embeddings, an encrypted FAISS gallery cache, policy metadata, review alerts, analytics, and CSV exports.

> [!CAUTION]
> This repository is **not launch-ready for biometric decisions**. Its embedding model operates on full images because the supplied data has no verified muzzle bounding-box annotations or trained muzzle detector. Dataset identity provenance and population representativeness are unverified. Prototype outcomes are not proof of ownership and must not determine insurance, fraud, veterinary, or other consequential outcomes. Always require independent human confirmation.

## Current functionality

| Capability | State |
|---|---|
| Sign-in, animal registry, create/edit/delete, authenticated image previews | Available |
| Encrypted uploaded images and embedding vectors | AES-GCM, with per-record associated data |
| Technical image checks | Format, decoded size, animation, dimensions, corruption, plus non-decision capture-quality flags |
| ResNet-50 embedding training and inference | Available after local training artifacts are installed |
| Validation and test split | Animal-identity-disjoint train, checkpoint selection, calibration, and test partitions |
| FAISS vector search | Per-animal prototype index, encrypted persistence, model/version/content validation, rebuild from database records |
| VERIFIED / REVIEW / UNKNOWN | Calibrated cosine thresholds; prototype only, not a production biometric guarantee |
| Policy register | Operator-entered policy metadata only; no insurer integration or claims/eligibility decision |
| Alerts | Duplicate ear tags and REVIEW verification events; operator resolution workflow |
| Analytics and reports | Saved-record aggregates and formula-safe CSV exports |
| YOLO muzzle detection, validated crop pipeline, population-level biometric release evidence | **Not available**; verified muzzle annotations, data provenance, and independent evaluation are required |
| Multi-user roles, organization isolation, MFA, key-management service | Not available; restrict this prototype to one trusted operator |

Model files and raw datasets are local-only and excluded from Git. A clone therefore needs to run training before biometric inference is available.

## Install

Use Python 3.10–3.14 and a virtual environment. Install PyTorch using the official [PyTorch installation selector](https://pytorch.org/get-started/locally/) for the machine’s operating system and CPU/GPU. Then install the app and model dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-ml.txt
```

For the tests, also install `requirements-dev.txt`.

## Configure secrets

Copy `.env.example` to `.env` and create three independent random values:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))" # operator password
python -c "import secrets; print(secrets.token_urlsafe(32))" # session signing secret
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" # AES-GCM key
```

Set `MUZZLEID_OPERATOR_PASSWORD`, `MUZZLEID_SESSION_SECRET`, and `MUZZLEID_DATA_KEY`. The password must have at least 12 characters; the session secret must have at least 32; the data key must encode exactly 32 random bytes. Keep `.env` private and out of Git. Back up the data key securely: losing it makes encrypted images, embeddings, and the saved gallery cache unreadable. Rotate it only with a tested re-encryption procedure.

`COOKIE_SECURE=true` is required behind HTTPS. The default SQLite database is suitable only for local single-operator use. Metadata and audit fields are not application-encrypted; use host disk encryption and encrypted backups.

## Train and evaluate

Training reads only `data/train/dataset/<identity>/*`. It ignores byte-identical duplicates and assigns whole identity folders to 70% training, 10% checkpoint selection, 10% calibration, and 10% final testing. The latter partitions include enrolled identities and distinct unknown identities. Dataset folders are treated as labels; the provenance and real-world uniqueness of those labels are not independently verified.

The model is ImageNet-initialized ResNet-50 with a normalized 512-dimensional head, batch-hard triplet loss, and fine-tuning of the final ResNet block and embedding head. Training defaults to 20 bounded epochs with 128-pixel inputs to support CPU-only hosts. Adjust `--batches-per-epoch`, `--batch-identities`, or `--image-size` only as an explicit resource/experiment choice; record the settings alongside metrics.

```powershell
python -m src.training.train --epochs 20 --device auto
```

The first run needs the ImageNet initialization weights. The trainer writes `models/best_model.pt` and `models/calibration.json`; it reports identity-disjoint test top-1, known false-reject, and unknown false-accept rates. Unknown-query maximum cosine scores in the separate calibration identities set REVIEW and VERIFIED thresholds. These estimates are limited by the dataset and number of independent cattle identities. Do not tune thresholds against the final test partition.

The separate `data/test` directories are not included in training or the reported training-tree test metrics because their identity mapping and provenance relative to training are unknown. No muzzle-box annotations were found; a YOLO muzzle detector cannot be truthfully trained from the current images alone. The latest local run's metrics and limits are recorded in [the implementation report](reports/final_system_report.md); its unknown-query false-accept result fails the safety gate, so this model must remain a research prototype.

## Run locally

After installing dependencies, configuring `.env`, and (for matching) completing a successful training run:

```powershell
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in. Animal records can be stored without model artifacts and will show `pending_model`; verification remains unavailable until the checkpoint, matching calibration, and FAISS are present. When artifacts are added or replaced, open an animal profile and choose **Rebuild biometric profile** to generate versioned embeddings from its integrity-checked encrypted reference images. The system retains earlier model versions until the record is deleted.

## Docker Compose

Set `POSTGRES_PASSWORD` and the three MuzzleID secrets in `.env`, train the model locally, and run:

```powershell
docker compose up --build
```

Compose uses PostgreSQL, the CPU PyTorch runtime, a non-root API user, a read-only container root filesystem, dropped Linux capabilities, and a localhost-only published port. Model artifacts are mounted read-only; encrypted uploads and gallery cache use separate named volumes. Configure a TLS reverse proxy and set `COOKIE_SECURE=true` before exposing the service to a network. Do not expose Uvicorn directly to the public internet.

## Data integrity and recovery

Alembic migrations preserve the earlier MVP schema and add current columns. On startup the service detects missing referenced upload files, disables those animal profiles, removes their now-unusable embeddings, and logs the count. Stale temporary uploads and old unreferenced encrypted files are cleaned after a grace period.

The relational database is the source of truth for gallery embeddings. FAISS prototypes are rebuilt from authenticated/decrypted model-versioned records whenever the stored index fingerprint is stale, corrupt, or missing. The serialized index and identity mapping are AES-GCM encrypted and atomically replaced. A database or data-key backup must accompany any deployment recovery plan.

## API overview

Every data route requires the operator session.

| Method | Route | Function |
|---|---|---|
| `GET` | `/api/session` | Session/configuration state |
| `POST` | `/api/login`, `/api/logout` | Operator session management |
| `GET` | `/api/health`, `/api/settings` | Database, model, detector, FAISS and security configuration state |
| `GET`, `POST` | `/api/animals` | Search/paginate registry and register encrypted references |
| `GET`, `PATCH`, `DELETE` | `/api/animals/{id}` | Read, edit, or remove a record |
| `POST` | `/api/animals/{id}/reindex` | Rebuild current-model embeddings from verified stored images |
| `GET` | `/api/animals/{id}/images/{image_id}` | Authenticated decrypted reference preview, no-store |
| `POST` | `/api/verify` | Prototype-only identity result and persisted history |
| `GET` | `/api/history`, `/api/alerts`, `/api/analytics` | Audit and operational views |
| `GET`, `POST`, `PATCH`, `DELETE` | `/api/insurance/policies` | Operator-maintained policy metadata |
| `GET` | `/api/reports/animals.csv`, `/api/reports/verifications.csv` | Spreadsheet-safe exports |

`/docs` provides the API schema. Do not enable it on a public deployment without an access-controlled operations policy.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider tests
python -m compileall -q src tests migrations
node --check web\api.js
node --check web\app.js
docker compose config -q
python -m scripts.release_e2e
```

Before any real-world identity use, obtain verified identity provenance, collect and review muzzle-box annotations, train and validate a detector/crop contract, evaluate false-accept and false-reject rates on an untouched representative population, implement safe key rotation/re-enrollment/deletion controls, and conduct independent security and deployment testing.
