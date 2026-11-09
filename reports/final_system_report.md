# MuzzleID biometric accuracy overhaul — system report

**Date:** 2026-11-09

## Changes made

- Replaced animal-centroid flat lookup with encrypted, versioned FAISS HNSW retrieval over individual image references and aggregation to distinct animal candidates.
- Added up to three ranked candidates with reference image links, per-candidate status, image-quality summary, and explicit disclosure that no muzzle detector/crop is available.
- Added an incremental in-memory index update on registration, with the database remaining the source of truth and rebuild/recovery at startup.
- Made the serving decision fail closed: the prototype calibration cannot return `VERIFIED`; explicit release approval, candidate margin, and agreement across references are required. The unused legacy matcher is also kept at `REVIEW`/`UNKNOWN`.
- Added regression tests for HNSW candidate diversity, reference aggregation, incremental insertion, candidate decision gating, and API candidate responses.
- Completed the requested pre-change technical audit in `reports/accuracy_audit.md`.

## Accuracy evidence and release status

The historical baseline recorded a 13.68% unknown false-accept rate (16/117 queries from only 13 unknown identities). This remains unsuitable for automatic identity verification. EfficientNet, ArcFace, and DINOv2 comparisons, detector/crop training, and a fresh fully labeled biometric benchmark were not run because pretrained weights, detector labels, identity provenance, and CUDA compute are unavailable locally. No comparison winner or new accuracy improvement is claimed.

Automatic verification remains disabled for the current model. Results are candidate suggestions for human review only and must not be used to establish ownership or insurance eligibility.

## Validation status

The project test suite passed: **31 passed**, with one Starlette/httpx deprecation warning. The real-model release E2E passed using the local checkpoint and identity-disjoint sample paths: known query ranked its enrolled identity at cosine 0.999425 with decision `REVIEW`; one unknown query returned `UNKNOWN` at 0.060614; invalid image returned HTTP 422; reindexing, restart recovery, encrypted index recovery, and corrupt-index rebuild all passed. This small one-known/one-unknown exercise is a serving regression, not a biometric accuracy estimate. The synthetic HNSW benchmark completed at 1k and 10k vectors; the 100k build was interrupted after exceeding the local run window.

## Remaining work before release

Use a provenance-reviewed, capture-session-aware identity dataset; train and compare candidate architectures under one locked protocol; validate a muzzle detector and quality/visibility gate; measure exact/approximate recall and application performance on representative galleries; then independently review operating thresholds and false-accept confidence bounds before setting `verification_approved`.
