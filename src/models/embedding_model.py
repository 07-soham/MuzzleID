"""ResNet-50 metric-learning embedding model (requires the optional torch stack)."""
try:
    import torch
    from torch import nn
    from torchvision.models import ResNet50_Weights, resnet50
except ImportError as exc:  # Keep API and audit importable without ML dependencies.
    torch = None
    _TORCH_ERROR = exc


def build_model(pretrained: bool = True):
    if torch is None:
        raise RuntimeError("Install the ML dependencies from requirements-ml.txt to use the embedding model.") from _TORCH_ERROR

    class ResNet50Embedder(nn.Module):
        def __init__(self):
            super().__init__()
            backbone = resnet50(weights=ResNet50_Weights.IMAGENET1K_V2 if pretrained else None)
            self.features = nn.Sequential(*list(backbone.children())[:-1])
            self.embedding = nn.Linear(backbone.fc.in_features, 512)

        def forward(self, images):
            features = self.features(images).flatten(1)
            return nn.functional.normalize(self.embedding(features), p=2, dim=1)

    return ResNet50Embedder()
