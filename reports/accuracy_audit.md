# MuzzleID biometric accuracy audit

**Audit date:** 2026-11-09  
**Scope:** Existing training, image preprocessing, inference, calibration, retrieval, API, UI, and available datasets. This report records the pre-overhaul state; no biometric implementation changes were made as part of the audit.

## Executive finding

The current system is not reliable enough to verify animal identity. The latest checked-in evaluation artifact records a **13.68% unknown-animal false-accept rate (16/117)** on its small internal open-set test. A nearest candidate is converted to `VERIFIED` solely from a cosine threshold. The encoder sees the full image rather than a muzzle crop, so it can use background, harness, coat, lighting, or other scene cues. The existing release report already correctly marks the system as not launch-ready for identity decisions.

The root cause is not demonstrably a single bad threshold. It is the combination of full-frame shortcut risk, limited and weakly documented identity data, a small metric-learning setup, a calibration sample too small to substantiate its tail threshold, and a decision rule that uses only the best cosine score. The present evidence does not establish which feature caused any individual false match.

## Model and training audit

- `src/models/embedding_model.py` implements an ImageNet-initialized ResNet-50 followed by a learned linear 512-dimensional head and L2 normalization. The checkpoint metadata in the current calibration describes `resnet50-linear-512-l2`, model version `d04c1ccd7ff1aeaa`; `models/best_model.pt` is a local 98.5 MB artifact.
- `src/training/train.py` uses batch-hard triplet loss with margin 0.2 and identity-balanced batches. The recorded run used 8 identities × 4 images, 16 batches per epoch, 10 CPU epochs, 128-pixel inputs, and seed 2026. It freezes the backbone except for ResNet layer 4 and the embedding layer. Training augmentation is horizontal flip only.
- The training tree currently has 4,943 images in 268 folder labels (4–70 files per label; median 16). The previous content audit found 4,923 unique byte contents and 20 exact duplicate groups; its listed duplicate examples are copies within the same cattle folder. The train loader deduplicates file bytes globally, but folder labels and source identity provenance are not externally verified.
- Training and inference both apply EXIF orientation, RGB conversion, direct square resize, and ImageNet mean/std normalization. Directly warping a non-square full frame to 128×128 can distort the muzzle. No muzzle detector, crop labels, lighting correction, or occlusion classifier is present. Quality checks cover decode safety, dimensions, and heuristic blur/brightness/contrast warnings; those checks do not determine whether the muzzle is visible.
- No EfficientNet, ArcFace, or DINOv2 implementation or same-protocol architecture comparison exists in this repository. Therefore there is no evidence that the current backbone is better than the requested alternatives. The current system cannot train ArcFace as configured; its loss is batch-hard triplet loss.

## Dataset and label audit

- **Training:** `data/train/dataset` currently contains 4,943 images under 268 identity-named folders. These counts match the prior `reports/dataset_report.json` scan. The current full-file duplicate scan was not repeated because the separate test tree contains large, high-resolution images; the duplicate details below come from that dated prior scan.
- **External test tree:** Each of `data/test/Cattle Muzzle - DB/Modified` and `.../Original` currently has 1,486 image files across 24 numeric identity folders plus a `ModifiedMaster` or `OriginalMaster` folder. The previous audit found 743 identity-labeled examples and 743 exact master copies per representation. Master copies must be excluded from independent scoring to avoid duplicate observations. Modified and Original image sets have different resolutions and represent distinct preprocessing/domain conditions.
- Train labels (`cattle_####`) and external test labels (`1`–`24`) use different namespaces. No identity crosswalk is present. Literal folder-name disjointness therefore does not prove that the underlying animals differ. Until provenance is confirmed, external identities are only candidate open-set labels, not a validated same-animal benchmark.
- The folder `data/real life dataset` contains 27 photographs: 21 at its root and six in `set1`. Root-level filenames are mostly capture timestamps plus a few `cow1`/`cow2` names; the six files in `set1` share a directory, which is not sufficient evidence that they depict one individual. Capture session metadata and reviewed identity labels are absent.
- The local `cow1.jpeg`, `cow1 (2).jpeg`, and `cow2.jpeg` examples visually show different framing and cattle. Filenames alone do not establish identity. In particular, the brief’s negative-pair labels have not been independently confirmed, so no negative-pair regression assertion can yet be truthfully added.
- `reports/dataset_report.json` was generated 2026-10-02. Its train/test counts match current directory-level counts, but its image hashes, resolution scan, and corrupt-file scan are historical and have not been recomputed for this audit.
- No reliable session IDs, annotation manifest, or muzzle bounding-box labels were found in the inspected dataset tree. A verified detector-training set is therefore not available in the current project.

## Evaluation and calibration audit

The checked-in `models/calibration.json` records an identity-folder-disjoint split of the training tree: 187 training identities, 27 validation identities, 14 enrolled and 13 unknown calibration identities, and 14 enrolled and 13 unknown test identities. The earlier `reports/final_system_report.md` records these resulting metrics:

| Metric | Recorded result |
|---|---:|
| Validation known-identity top-1 | 100.00% |
| Held-out known-identity top-1 | 100.00% |
| Held-out known queries verified correctly | 125/126 (99.21%) |
| Held-out known false rejects | 1/126 (0.79%) |
| Unknown queries falsely accepted | **16/117 (13.68%)** |
| Unknown queries sent to review | 10/117 (8.55%) |
| Unknown queries rejected | 91/117 (77.78%) |
| Review / verified cosine thresholds | 0.50152 / 0.55271 |

These are recorded prototype results, not independently reproduced in this audit. The effective number of unknown animals is only 13, even though there are 117 query files. The 99.5th percentile calibration tail is estimated from 109 query files belonging to 13 identities; it cannot establish a high-confidence low FAR. Thresholds were selected using calibration identities, and the existing code does not tune on the external test tree. No ROC-AUC, ROC curve, EER, TAR@FAR, pairwise genuine/impostor distribution, confidence interval, hard-negative report, or session-level analysis is recorded. The reported 100% known top-1 is a closed-enrolled candidate-ranking result and does not offset the unknown false accepts.

## Retrieval, decision, API, and product audit

- Registration stores separate encrypted per-image embeddings with a model version, which is useful. However, the admin FAISS builder averages each animal’s reference embeddings into one normalized center, losing multi-modal variation. The candidate is then selected from one animal-level center.
- `src/inference/faiss_gallery.py` builds `faiss.IndexFlatIP` and searches `k=1`. This is exact flat search, not HNSW/IVF approximate retrieval. `/api/verify` also loads current embeddings from the database and fingerprints the gallery to validate the cache, work proportional to the gallery size. The farmer verification path bypasses FAISS and iterates over every owned reference vector on each query. There is no measured large-gallery latency, memory, or recall benchmark.
- `decide()` has `VERIFIED`, `REVIEW`, and `UNKNOWN` thresholds, but bases the result only on the best score. It does not use candidate margin, agreement among captures, image-quality warnings, or ambiguity. Cosine is displayed as a similarity score, not a calibrated probability.
- `/api/verify` returns one `matched_animal`; `/api/farmer/verify` likewise returns at most one. The UI displays that one match and score. It does not retrieve or display three distinct candidates, their reference images, a detected crop, or per-candidate review status.
- The API uses the general upload-quality validator but has no muzzle visibility assessment. Images can be accepted for embedding even when the nose is small, occluded, or surrounded by a halter. Review logging exists for the `REVIEW` decision, but audit reasons are generic rather than feature/quality diagnostics.
- Registration supports multiple reference images for a newly created animal. It does not offer an add-reference-images workflow for an existing identity. Duplicate image bytes are rejected within one registration; evidence-based duplicate-animal detection is not present.

## Regression and security test coverage

Existing tests cover image decode/size checks, a basic known/unknown release flow, role isolation, encrypted storage/recovery, index corruption recovery, and a few ranking/index invariants. They do not cover labeled same-animal capture changes, confirmed different-animal hard negatives, breed or lighting variation, partial muzzle obstruction, blur-to-review decisions, top-three distinct retrieval, duplicate candidate aggregation at the API/UI boundary, representative 100k+ galleries, ROC/EER/TAR/FAR, or model/index mismatch through the full serving pipeline.

Existing authentication, owner filtering, request-size limits, encrypted images/embeddings, path safety, and model/calibration version checks should remain in force while accuracy changes are made. A higher threshold alone is not a fix: the current system already fails the unknown false-accept test at its recorded thresholds.

## Required inputs and audit limits

Before accepting a negative-pair regression test, a human dataset owner must identify the two files and confirm they show different individual cattle. A reliable biometric release also needs verified identity provenance, session/capture metadata, muzzle boxes or a validated detector, and a target FAR with an agreed confidence bound. Architecture selection needs the same identity-disjoint train/validation/calibration/test protocol and available pretrained weights. Until those inputs exist and new measurements pass, the safe policy is to treat output as a candidate suggestion and route uncertain evidence to review, never as proof of identity or insurance eligibility.
