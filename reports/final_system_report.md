# MuzzleID release verification report

## Release status

**Not launch-ready for biometric identity decisions.** Core prototype workflows, software tests, model loading, calibration, and FAISS recovery passed local checks, but the held-out unknown-animal false-accept rate was 13.68% (16 of 117 queries). The model uses full images because the dataset has no verified muzzle boxes or detector labels. Label provenance and population representativeness are also unverified. Do not rely on prototype results for ownership, insurance, fraud, veterinary, or other consequential decisions.

## Biometric engine configuration diagnosis

On the current workstation, the configured model and calibration files exist and load as matching version `d04c1ccd7ff1aeaa`; FAISS imports, and authenticated live `/api/health` reports `verification_available: true`. The configuration error is reproducible on deployments without these artifacts: `models/` is intentionally excluded from Git, while Compose expects `./models/best_model.pt` and `./models/calibration.json` mounted at `/app/models`. The service previously only checked model availability when health was requested and did not initialize the model/gallery during startup. Startup now validates and warms the model/calibration pair, loads or rebuilds the encrypted FAISS index and animal mapping from persistent embeddings, logs the precise preflight failure, and exposes `biometric_engine_reason` in health responses. A clean clone must provision the paired artifacts; the training dataset is also local-only and is not available for automatic retraining in a fresh checkout.

## Implemented

- Added Alembic schema history, including a migration from one embedding per image to model-versioned embeddings while preserving old vectors.
- Added encrypted, fingerprinted FAISS persistence with identity mapping, atomic replacement, and automatic recovery from missing, stale, corrupt, or key/version-mismatched caches.
- Added per-animal reindexing from encrypted, SHA-256-verified reference images. Registry, details, analytics, and CSV report profile states now reflect embeddings for the current model version.
- Added image format, readability, bounds, animation, and non-decision capture-quality checks; stored quality metadata for review.
- Trained and evaluated an ImageNet-initialized ResNet-50 with a 512D normalized embedding head and identity-disjoint partitions. Kept the separate `data/test` tree out of training because its identity mapping is not verified.
- Updated Docker configuration with PostgreSQL, model dependencies, non-root/read-only runtime settings, local-only port binding, secret-safe database URL construction, and persistent data volumes.
- Updated README with setup, model limitations, reindex workflow, API routes, recovery guidance, and verification commands.
- Added automatic startup engine preflight and current-version FAISS gallery restoration; health reports readiness only after database, model/calibration, and FAISS checks pass.

## Model run and held-out metrics

Run: CPU, 10 epochs, 128-pixel inputs, 8 identities × 4 images per batch, 16 batches per epoch, seed 2026. Partitions: 187 training identities, 27 validation identities, then calibration and test partitions each split into 14 enrolled and 13 unknown identities. There were 3,497 training images. Calibration used 109 unknown queries. The final test used 126 known queries and 117 unknown queries.

| Metric | Result |
|---|---:|
| Validation known-identity top-1 | 100.00% |
| Test known-identity top-1 | 100.00% |
| Test known queries verified correctly | 99.21% (125/126) |
| Test known false reject | 0.79% (1/126) |
| Test unknown false accept | **13.68% (16/117)** |
| Test unknown routed to review | 8.55% (10/117) |
| Test unknown rejected | 77.78% (91/117) |
| Review / verified cosine thresholds | 0.50152 / 0.55271 |

These are small-sample, folder-identity-disjoint metrics on the current training tree, not independent real-world performance evidence. The 13.68% unknown false-accept rate fails the release gate. The checkpoint and calibration remain local, ignored artifacts and are not distributed in Git.

## Verification performed

| Command or check | Result |
|---|---|
| `python -m pytest -q -p no:cacheprovider --tb=short tests` | 13 passed |
| `python -m scripts.release_e2e` | Passed: startup preflight, actual checkpoint registration, reindex, known match, unknown rejection in selected case, invalid-image HTTP 422, encrypted image retrieval, record/history restart persistence, startup FAISS mapping recovery, corrupt-cache rebuild |
| `python -m compileall -q src migrations scripts tests` | Passed |
| `node --check web\api.js` and `node --check web\app.js` | Passed |
| `python -m alembic current` | `0004_versioned_embeddings (head)` |
| `docker compose config -q` | Passed |
| Live service startup and authenticated health | Login 200; database/model/FAISS ready, `verification_available: true`; listener stopped afterward |
| Live loopback smoke check | Homepage 200, app JS 200, configured session, unauthenticated health 401; listener stopped afterward |
| `docker info` / Docker deployment run | **Blocked:** Docker Desktop Linux Engine named pipe is unavailable in this environment |
| Real YOLO detector validation | **Blocked:** no verified muzzle bounding-box annotations/model artifacts |
| Frontend browser automation | Not run; app/API asset and client syntax checks were run, plus API end-to-end workflows |

The E2E restart check creates a fresh FastAPI lifespan and clears the in-process FAISS cache while reusing a temporary persistent SQLite database and index. It does not simulate a host power loss or an independent OS-process restart.

## Remaining launch blockers

1. Obtain verified identity provenance and representative cattle data; evaluate on an independently collected population with a pre-registered false-accept target and confidence bounds.
2. Collect expert-reviewed muzzle boxes, train and validate a detector, and test crop quality and downstream biometric performance.
3. Reduce the unknown false-accept rate and independently review calibration before any biometric release.
4. Start Docker Engine and exercise actual PostgreSQL Compose deployment, database backup/restore, and host/process restart recovery.
5. Complete a browser-based UI smoke test and independent production security review. Current deployment is single-operator and does not include MFA, multi-tenant authorization, KMS/key rotation, or externally managed rate limits.

Until those gates pass, keep the application restricted to local prototype/research use with human confirmation.
