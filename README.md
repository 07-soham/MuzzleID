# MuzzleID

**A secured prototype for cattle records and muzzle-image identity research.**

MuzzleID combines a FastAPI service, an operator portal, encrypted image and embedding storage, and a reproducible ResNet-50 metric-learning training workflow. The model is intended for research and demonstration only.

> [!WARNING]
> Prototype identity suggestions are not reliable evidence of ownership. The current pipeline embeds the full image, has no muzzle detector or crop, and has not been validated on identities known to be absent from training. Do not use it for insurance, fraud, veterinary, ownership, or other consequential decisions. Require human confirmation.

## Current capabilities

| Area | Status |
|---|---|
| Operator portal, registry search, registration, details, system status | Available after operator sign-in |
| Registration and gallery reference images | AES-GCM encrypted at rest; max 8 images × 12 MiB |
| Operator sessions | HttpOnly, SameSite=Strict signed cookie; configured password and session secret required |
| Upload checks | Format allowlist, decoding, dimensions, animation rejection, request/image limits |
| Model training | Reproducible train/validation split over `data/train/dataset`; exact byte duplicates are kept on one side |
| Embedding network | ImageNet-initialized ResNet-50 with a 512-dimensional normalized embedding head |
| Training objective | Batch-hard triplet loss with identity-balanced batches |
| Validation | Checkpoint selection by known-identity top-1; thresholds estimated from validation impostor-score quantiles |
| Gallery and verification | Encrypted database embeddings and cosine prototype search; results clearly labeled prototype-only |
| Muzzle detection, crop, open-set evaluation, external test evaluation | Not available; labels/provenance needed to implement and assess these safely |
| Multi-user roles, organization isolation, migrations, retention controls | Not available |

No model weights or calibration file are included in Git. They are generated locally by training and ignored by `.gitignore`.

## Security setup

The service refuses to issue an authenticated session without an operator password and a 32-byte session secret. It also refuses image registration and gallery inference unless a valid 32-byte encryption key is configured. Generate three independent values:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))" # operator password
python -c "import secrets; print(secrets.token_urlsafe(32))" # session secret
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" # data key
```

Copy `.env.example` to `.env`, then set `MUZZLEID_OPERATOR_PASSWORD`, `MUZZLEID_SESSION_SECRET`, and `MUZZLEID_DATA_KEY` to the generated values. Keep `.env` private and out of Git. Losing or rotating the data key without re-encrypting records makes stored images and vectors unreadable. Back it up securely.

Uploads and embedding vectors are encrypted with AES-GCM, with per-record associated data. The database stores image hashes and encrypted vectors. Login, registration, and verification have per-process rate limits. The browser session cookie is `HttpOnly` and `SameSite=Strict`; enable `COOKIE_SECURE=true` behind HTTPS. Compose publishes only on `127.0.0.1` by default. Do not expose the development server directly to the public internet.

For an existing local install with unencrypted uploads, back up the database and upload directory, then preview and apply the migration after setting the data key:

```powershell
python -m src.security.migrate_storage
python -m src.security.migrate_storage --apply
```

This is a single-operator prototype, not a hardened multi-tenant service. Rate limits are process-local, there is no MFA/role system, audit retention policy, key-management service, malware scanner, or database migration framework. Restrict host access, keep backups encrypted, and put a properly configured HTTPS reverse proxy and firewall in front of any shared deployment.

## Install and train

Use a virtual environment. The training script requires a platform-compatible PyTorch and torchvision build; follow the official [PyTorch installation selector](https://pytorch.org/get-started/locally/) for CPU or the correct GPU/CUDA version.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-ml.txt
```

`data/train/dataset/<identity>/*` is the only dataset read by training. The separate `data/test` tree is deliberately not used: its numeric identities have not been mapped to the training labels, so identity-level leakage cannot be ruled out. The script deduplicates exact file bytes before splitting each training identity into train and validation images.

```powershell
python -m src.training.train --epochs 20 --device auto
```

The first run downloads torchvision's ImageNet initialization weights. Training selects a checkpoint using held-out known-identity top-1 accuracy, then estimates review/verified cutoffs from the 95th and 99.5th percentiles of each validation query's best impostor-identity score. It writes:

```text
models/best_model.pt
models/calibration.json
```

The validation split contains the same identities as training, so these cutoffs do not measure false matches against genuinely unseen cattle. A quantile is not a safety guarantee. Full-frame embeddings can learn background or acquisition shortcuts. Model and calibration metrics are diagnostic prototype metrics, not production performance claims.

If the machine has no compatible GPU, `--device cpu` works but may take a long time. For GPU use, install the matching build selected by PyTorch's official installer; `--device auto` uses CUDA when available.

## Run locally

After configuring `.env` and (optionally) training a model:

```powershell
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), then sign in with the operator password. `/docs` exposes the API schema; data endpoints still require the session cookie.

Without trained weights, animal records can still be created and saved as `pending_model`, with images encrypted. After the model and matching calibration are installed, new registrations create encrypted reference embeddings and are labeled `prototype_ready`. Previously pending records are not silently re-embedded; an explicit migration/re-enrollment workflow is still needed. Model version changes also require re-enrollment because the gallery is versioned.

## Docker Compose

Set the database password and all three security values in `.env`, train the model locally, then run:

```powershell
docker compose up --build
```

The API binds to localhost. Compose mounts `models/` read-only and stores encrypted uploads and PostgreSQL data in named volumes. For an HTTPS deployment, configure the reverse proxy and set `COOKIE_SECURE=true`. Docker runtime and GPU acceleration must be configured for the host; this repository does not configure GPU containers.

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/session` | Reports whether security settings exist and the browser session is authenticated |
| `POST` | `/api/login` | Starts an eight-hour operator session |
| `POST` | `/api/logout` | Clears the operator session |
| `GET` | `/api/health` | Authenticated database/model/calibration status |
| `GET` | `/api/animals?q=&offset=&limit=&sort=` | Authenticated searchable, paginated registry |
| `GET` | `/api/animals/{id}` | Authenticated animal details and image metadata |
| `POST` | `/api/animals` | Authenticated registration; requires `Idempotency-Key` and 1–8 `images` |
| `POST` | `/api/verify` | Authenticated prototype match; persists a labeled prototype result |
| `GET` | `/api/history` | Authenticated recent verification results |

## Data and project layout

```text
src/api/          FastAPI routes, sessions, rate limits, security headers
src/database/     SQLAlchemy records and encrypted embedding table
src/inference/    Image validation, artifact loading, embeddings, prototype search
src/models/       ResNet-50 embedding model and triplet loss
src/security/     AES-GCM record encryption
src/training/     Train/validation split, training, checkpoint selection, calibration
web/              Static operator portal
tests/            API, security, image and decision tests
data/             Local-only dataset; excluded from Git
models/           Local-only trained weights and calibration; excluded from Git
```

Animal reference images are stored under `storage/uploads` using generated names and encrypted bytes. Per-image embeddings are encrypted in the database and tagged with a model version. The prototype gallery is searched with cosine similarity; FAISS is not currently used.

## Checks

```powershell
python -m pytest -q -p no:cacheprovider tests
python -m compileall -q src tests
node --check web\api.js
node --check web\app.js
```

## Next work before real identity use

1. Obtain verified identity provenance and a valid identity-disjoint evaluation split.
2. Acquire muzzle bounding-box annotations and train/validate a detector and preprocessing contract.
3. Measure open-set FAR/FRR, confidence intervals, acquisition variation, and demographic/breed performance on untouched cattle identities.
4. Add explicit re-enrollment, deletion, key rotation, retention, database migrations, roles and organization isolation.
5. Threat-model and penetration-test the deployed service before using sensitive operational records.
