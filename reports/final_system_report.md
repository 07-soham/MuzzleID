# MuzzleID prototype implementation report

## Product status

MuzzleID now has a secured operator portal and API, AES-GCM storage for reference images and embeddings, a ResNet-50 metric-learning training command, validation-based checkpoint selection, threshold calibration, model-versioned gallery records, and prototype-only verification.

The system is still a research prototype. It embeds the whole image; there is no trained muzzle detector or crop. Calibration uses a held-out image split from the same training identities. The separate test folders remain unused because their numeric label IDs have no verified mapping to the training labels. No claim about unseen-cattle false-accept performance or production identity accuracy is supported.

## Dataset

The local training partition has 4,943 JPEG files under 268 cattle identity folders. The prior read-only audit found 20 exact duplicate groups. The training script removes byte-identical images before making per-identity train/validation splits. The test directories and their mirrored master folders are not read by training or calibration.

Raw data and image-level audit JSON are excluded from Git. Aggregate-only dataset notes may be committed.

## Model workflow

- Architecture: ImageNet-initialized ResNet-50, 512D L2-normalized embeddings.
- Objective: batch-hard triplet loss with identity-balanced batches.
- Checkpoint selection: highest held-out known-identity top-1; training loss breaks ties.
- Decision cutoffs: validation impostor-score 95th/99.5th percentiles for review/verified.
- Gallery: encrypted per-reference embeddings in the database; cosine similarity over per-animal mean prototypes.
- Test data: not used.
- Detector: unavailable because no muzzle bounding-box annotations are present.
- Thresholds: not a guarantee; same-identity image holdout is not an open-set benchmark.

The initial full training run is being executed locally on CPU because this environment has no CUDA device. Final run metrics are recorded here only after the process completes. Model weights and calibration remain local and ignored by Git.

## Security and data handling

- Operator password and session key are stored in local ignored `.env`; signed eight-hour HttpOnly/SameSite=Strict cookie sessions protect all data routes.
- Browser/API data routes reject unauthenticated calls. Login, registration and verification are rate-limited per process.
- Request body limits apply while streaming; supported images are JPEG, PNG or WebP, single-frame, decoded and bounded to 40 million pixels.
- Reference images and stored embeddings are AES-GCM encrypted with per-record associated data. The data key is local and must be backed up securely; losing it makes those records unreadable.
- The 12 existing local image files were encrypted in place and verified against their stored SHA-256 digests. No image content was logged or added to Git.
- Local Docker port mapping binds only to `127.0.0.1`; `COOKIE_SECURE=true` is required behind HTTPS.
- Animal metadata and image digests in SQLite/PostgreSQL are not application-encrypted. Use host disk encryption and encrypted backups.

This prototype has no MFA, multi-user roles, organization isolation, key-management service, malware scanner, retention/deletion workflow, or database migration framework. Rate limiting is process-local. Do not expose the development service to the public internet.

## Verification

| Check | Result |
|---|---|
| Backend/API/security suite | 7 passed on 2026-10-15 |
| Python `compileall` | Passed |
| JavaScript `node --check` for portal and API client | Passed |
| Encrypted upload integrity | 12 records decrypted in memory and matched recorded SHA-256 digests |
| Docker Compose | Not run; Docker Engine was unavailable during the previous local verification |
| CUDA | Not available; training uses CPU |
| Open-set evaluation / detector / ownership accuracy | Not available; not supported by current labels or annotations |

## Run locally

See [README.md](../README.md) for secret setup, model training, the local server and Compose instructions. Training writes `models/best_model.pt` and `models/calibration.json`; neither file is committed.
