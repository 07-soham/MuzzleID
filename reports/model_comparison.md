# MuzzleID model comparison

**Run date:** 2026-11-09  
**Status:** Comparative training run blocked; no architecture winner can be selected from available evidence.

## Available baseline

The repository's existing encoder is an ImageNet-initialized ResNet-50 with a learned 512-dimensional L2 embedding. It is fine-tuned with batch-hard triplet loss. The saved checkpoint and calibration report historical held-out known top-1 of 100% (126 queries), unknown false accepts of 16/117 (13.68%), and a 0.55271 verified threshold. Those are prior recorded results, not results from a comparable fresh run in this report. The unknown set contains only 13 identities and does not establish reliable tail performance.

## Requested alternatives

| Candidate | Comparable run | Reason |
|---|---|---|
| ResNet-50 baseline | Existing artifact only; no fresh rerun | Existing train/calibration artifacts are present locally, but the dataset lacks identity/session provenance for a defensible fresh protocol. |
| EfficientNet | Not run | No same-protocol implementation/checkpoint is available in the local environment; pretrained weights are not cached. |
| ArcFace | Not run | Current trainer is triplet-loss based; ArcFace loss/head and a controlled run have not been implemented. |
| DINOv2 | Not run | No DINOv2 implementation or cached pretrained weights are available. |

The local environment has CPU-only PyTorch and no CUDA device. No model weights were downloaded and no new model was trained. Consequently there are no honest comparative accuracy, ROC-AUC, EER, TAR@FAR, training-time, or GPU-memory figures to report. A comparison would require an identity/session-reviewed split, fixed image protocol and compute budget, the same calibration procedure, and fresh held-out identities for each architecture.

## Decision

Retain the existing model only as a candidate-ranking prototype. Do not promote it or choose an alternative based on unavailable comparisons. The serving path now returns `REVIEW` for plausible candidates unless a calibration explicitly carries `verification_approved`, candidate margin and multi-reference agreement pass, and quality controls pass. The checked-in prototype calibration is not independently release-approved, so the new path cannot automatically verify animals.

## Required next experiment

Build a provenance-reviewed dataset with multiple capture sessions per identity, plus independently reviewed unknown animals. Freeze identity partitions before training. Compare ResNet-50, EfficientNet, ArcFace, and DINOv2 under identical train/validation/test identities and capture conditions. Report identity-level bootstrap confidence intervals, ROC-AUC/EER, TAR at prespecified FAR, known reject rate, unknown false-accept rate, quality-stratified results, and model/runtime resource use. Never tune on the final test identities.
