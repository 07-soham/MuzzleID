"""Batch-hard triplet loss for normalized identity embeddings."""
try:
    import torch
    from torch import nn
except ImportError as exc:
    torch = None
    _TORCH_ERROR = exc


def batch_hard_triplet_loss(embeddings, labels, margin: float = 0.2):
    if torch is None:
        raise RuntimeError("PyTorch is required for metric-learning loss.") from _TORCH_ERROR
    distances = torch.cdist(embeddings.float(), embeddings.float(), p=2)
    same = labels[:, None].eq(labels[None, :])
    same.fill_diagonal_(False)
    different = ~labels[:, None].eq(labels[None, :])
    valid = same.any(dim=1) & different.any(dim=1)
    if not valid.any():
        raise ValueError("Each training batch must contain at least two images per identity and two identities.")
    hardest_positive = distances.masked_fill(~same, float("-inf")).max(dim=1).values
    hardest_negative = distances.masked_fill(~different, float("inf")).min(dim=1).values
    return torch.relu(hardest_positive[valid] - hardest_negative[valid] + margin).mean()
