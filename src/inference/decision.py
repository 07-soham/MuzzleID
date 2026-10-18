"""Explicit three-way matching decision; thresholds must be calibrated from validation data."""
import math


def decide(similarity: float, *, verified_threshold: float, review_threshold: float) -> str:
    if not all(math.isfinite(value) for value in (similarity, verified_threshold, review_threshold)):
        raise ValueError("Similarity and decision thresholds must be finite numbers.")
    if not -1 <= similarity <= 1:
        raise ValueError("Cosine similarity must be between -1 and 1.")
    if not -1 <= review_threshold <= verified_threshold <= 1:
        raise ValueError("Require -1 <= review_threshold <= verified_threshold <= 1.")
    if similarity >= verified_threshold:
        return "VERIFIED"
    if similarity >= review_threshold:
        return "REVIEW"
    return "UNKNOWN"
