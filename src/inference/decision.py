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


def decide_candidates(candidates, calibration: dict) -> str:
    """Fail closed on ambiguous evidence and unapproved prototype calibration."""
    if not candidates:
        return "UNKNOWN"
    top = candidates[0]
    score = float(top["similarity"])
    review = float(calibration["review_threshold"])
    verified = float(calibration["verified_threshold"])
    if score < review:
        return "UNKNOWN"
    if not calibration.get("verification_approved", False):
        return "REVIEW"
    margin = float(calibration.get("minimum_candidate_margin", 1.0))
    support = int(calibration.get("minimum_reference_support", 2))
    support_scores = top.get("support_similarities", [])
    verified_support = sum(float(reference_score) >= verified for reference_score in support_scores) if support_scores else int(top.get("support_count", 0))
    second_score = float(candidates[1]["similarity"]) if len(candidates) > 1 else -1.0
    if score < verified or score - second_score < margin or verified_support < support:
        return "REVIEW"
    return "VERIFIED"


def candidate_statuses(candidates, calibration: dict, decision: str) -> list[str]:
    """Give each distinct animal its own conservative status label."""
    statuses = []
    review = float(calibration["review_threshold"])
    for rank, candidate in enumerate(candidates, start=1):
        score = float(candidate["similarity"])
        if rank == 1 and decision == "VERIFIED":
            statuses.append("VERIFIED")
        elif score >= review:
            statuses.append("REVIEW")
        else:
            statuses.append("UNKNOWN")
    return statuses


def decision_reason(candidates, calibration: dict, decision: str) -> str:
    if not candidates:
        return "No enrolled reference candidates were retrieved."
    top = candidates[0]
    if decision == "UNKNOWN":
        return "The nearest candidate did not meet the review threshold."
    if decision == "REVIEW" and not calibration.get("verification_approved", False):
        return "Automatic verification is disabled because this calibration has not passed an independent low-false-acceptance release review."
    if decision == "REVIEW":
        return "The score, reference agreement, or separation from the next candidate is insufficient for automatic verification."
    return "Validated thresholds and independent reference agreement were satisfied."
