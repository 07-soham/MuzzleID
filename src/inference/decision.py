"""Explicit three-way matching decision; thresholds must be calibrated from validation data."""


def decide(similarity: float, *, verified_threshold: float, review_threshold: float) -> str:
    if not 0 <= review_threshold <= verified_threshold <= 1:
        raise ValueError("Require 0 <= review_threshold <= verified_threshold <= 1.")
    if similarity >= verified_threshold:
        return "VERIFIED"
    if similarity >= review_threshold:
        return "REVIEW"
    return "UNKNOWN"
