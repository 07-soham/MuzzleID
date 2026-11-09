# MuzzleID biometric evaluation

**Run date:** 2026-11-09  
**Evaluation status:** Existing record audited; no new accuracy claim made.

## Existing recorded baseline

The prior `reports/final_system_report.md` records 100.00% known top-1 on 126 held-out known queries, with 125/126 known queries accepted and 1/126 rejected. On 117 unknown queries, 16 were falsely accepted (13.68%), 10 sent to review (8.55%), and 91 rejected (77.78%). Recorded review/verified cosine thresholds were 0.50152/0.55271.

These are historical results from 14 enrolled and 13 unknown test identities, not a reproduced result from this overhaul. Query counts must not be mistaken for independent cattle counts. The current data has no reviewed crosswalk or session-level provenance, and the external 24-folder test collection cannot be proven disjoint from training at the animal level. No genuine-pair/impostor-pair scores, ROC-AUC, EER, TAR@FAR, confidence intervals, or hard-negative analysis were present in the recorded artifacts.

## Serving regression behavior

The API now retrieves up to three distinct animal candidates from reference-image embeddings. Each candidate retains its best reference score and reference support. The score is cosine similarity, not a probability. The decision is fail-closed: without `verification_approved`, scores above the review threshold yield `REVIEW`, while scores below it yield `UNKNOWN`. Even an approved calibration needs a sufficient top-candidate margin and at least two references over the verified threshold to return `VERIFIED`.

The local calibration does not contain `verification_approved`. That is intentional: its recorded false-accept rate is not acceptable for automatic identity decisions. No verified identity accuracy is claimed for the modified serving path until its HNSW/index/API regression and labeled identity benchmark run in a fully working project test environment.

The real-model release E2E exercised one enrolled query and one identity-disjoint unknown query through registration, indexing, verification, restart, and index corruption recovery. It ranked the known identity first at 0.999425 (`REVIEW`) and rejected the one unknown at 0.060614 (`UNKNOWN`). These two queries only verify serving behavior; they are not statistically meaningful accuracy metrics. The 31 project tests pass, including HNSW persistence, API candidate diversity, and fail-closed behavior.

## Image and quality scope

Image ingestion decodes and normalizes common raster formats to JPEG. Quality checks report image-level warnings; there is no validated muzzle detector or muzzle visibility annotation set, so the system cannot provide a muzzle crop or establish that the muzzle is visible. The UI and API disclose this and require human review.

## Release gate

Keep automatic identity verification disabled. A release requires a fresh identity-disjoint and session-aware evaluation, independent labels, confidence intervals, low FAR at a prespecified operating point, error analysis for breed/lighting/occlusion/blur, and an independently reviewed calibration artifact. The current data and historical false-accept result do not meet that gate.
