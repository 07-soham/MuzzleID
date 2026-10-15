"""Train a reproducible closed-set embedding prototype on labeled training data.

Never points at data/test: only data/train/dataset is read. Calibration is
identity-disjoint at the image level but is not an open-set benchmark.
"""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
import random
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def load_stack():
    try:
        import torch
        from src.inference.engine import _load_torch
        from src.models.embedding_model import build_model
        from src.models.losses import batch_hard_triplet_loss
    except (ImportError, RuntimeError) as exc:
        raise SystemExit("Training needs compatible PyTorch and torchvision. Install requirements-ml.txt in Python 3.10 or 3.11.") from exc
    return torch, build_model, batch_hard_triplet_loss


def collect_split(dataset: Path, validation_fraction: float, seed: int):
    """Deduplicate byte-identical files, then split each identity deterministically."""
    groups = defaultdict(list)
    seen_hashes = set()
    if not dataset.is_dir():
        raise FileNotFoundError(f"Training dataset not found: {dataset}")
    for identity_dir in sorted(path for path in dataset.iterdir() if path.is_dir()):
        for path in sorted(identity_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                digest = sha256(path.read_bytes()).hexdigest()
                with Image.open(path) as image:
                    image.verify()
            except (OSError, ValueError):
                continue
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            groups[identity_dir.name].append(path)
    rng = random.Random(seed)
    training, validation = [], []
    for identity, paths in sorted(groups.items()):
        rng.shuffle(paths)
        if len(paths) < 3:
            continue
        holdout = max(1, min(len(paths) - 2, round(len(paths) * validation_fraction)))
        validation.extend((path, identity) for path in paths[:holdout])
        training.extend((path, identity) for path in paths[holdout:])
    if len({identity for _, identity in training}) < 2:
        raise ValueError("At least two identities with three unique readable images each are required.")
    return training, validation


class ImageDataset:
    def __init__(self, records, labels, image_size):
        self.records, self.labels, self.image_size = records, labels, image_size

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        import torch
        path, identity = self.records[index]
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB").resize((self.image_size, self.image_size), Image.Resampling.BILINEAR)
        array = np.asarray(image, dtype=np.float32) / 255.0
        array = (array - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
        # Light, deterministic-safe horizontal flip augments training only.
        tensor = torch.from_numpy(np.transpose(array, (2, 0, 1)).copy())
        if self.labels is not None and random.random() < .5:
            tensor = tensor.flip(-1)
        return tensor, self.labels[identity] if self.labels is not None else -1


class IdentityBatchSampler:
    def __init__(self, records, labels, identities_per_batch=16, samples_per_identity=4):
        self.by_identity = defaultdict(list)
        for index, (_, identity) in enumerate(records):
            self.by_identity[labels[identity]].append(index)
        self.identities_per_batch = min(identities_per_batch, len(self.by_identity))
        self.samples_per_identity = samples_per_identity
        self.batches = max(1, len(records) // (self.identities_per_batch * samples_per_identity))

    def __len__(self):
        return self.batches

    def __iter__(self):
        identities = list(self.by_identity)
        for _ in range(self.batches):
            chosen = random.sample(identities, self.identities_per_batch)
            yield [random.choice(self.by_identity[identity]) for identity in chosen for _ in range(self.samples_per_identity)]


def tensor_for(path: Path, image_size: int):
    import torch
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB").resize((image_size, image_size), Image.Resampling.BILINEAR)
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (array - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
    return torch.from_numpy(np.transpose(array, (2, 0, 1)).copy())


def embed_records(model, records, image_size, device, batch_size=32):
    import torch
    vectors = []
    model.eval()
    with torch.inference_mode():
        for start in range(0, len(records), batch_size):
            batch = torch.stack([tensor_for(path, image_size) for path, _ in records[start:start + batch_size]]).to(device)
            vectors.extend(model(batch).cpu().numpy())
    return np.asarray(vectors, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "train" / "dataset")
    parser.add_argument("--output", type=Path, default=ROOT / "models")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-identities", type=int, default=16)
    parser.add_argument("--samples-per-identity", type=int, default=4)
    parser.add_argument("--validation-fraction", type=float, default=.2)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if args.epochs < 1 or not 0 < args.validation_fraction < .5:
        parser.error("epochs must be positive and validation fraction must be between 0 and .5")
    torch, build_model, triplet_loss = load_stack()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    device = "cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu" if args.device == "auto" else args.device
    if device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA was requested but is unavailable")
    training, validation = collect_split(args.data, args.validation_fraction, args.seed)
    identities = sorted({identity for _, identity in training})
    label_ids = {identity: index for index, identity in enumerate(identities)}
    train_dataset = ImageDataset(training, label_ids, args.image_size)
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_sampler=IdentityBatchSampler(training, label_ids, args.batch_identities, args.samples_per_identity), num_workers=0)
    model = build_model(pretrained=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
    selector_gallery = []
    chosen = set()
    for record in training:
        if record[1] not in chosen:
            selector_gallery.append(record); chosen.add(record[1])
    best_accuracy = -1.0
    best_loss = float("inf")
    best_state = None
    for epoch in range(args.epochs):
        model.train(); losses = []
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad(set_to_none=True)
            embeddings = model(images)
            loss = triplet_loss(embeddings, labels)
            loss.backward(); optimizer.step()
            losses.append(float(loss.detach().cpu()))
        mean_loss = float(np.mean(losses))
        gallery_vectors = embed_records(model, selector_gallery, args.image_size, device)
        query_vectors = embed_records(model, validation, args.image_size, device)
        gallery_prototypes = {}
        for (_, identity), vector in zip(selector_gallery, gallery_vectors):
            gallery_prototypes[identity] = vector / max(float(np.linalg.norm(vector)), 1e-12)
        validation_correct = []
        for (_, identity), vector in zip(validation, query_vectors):
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            scores = {key: float(vector @ center) for key, center in gallery_prototypes.items()}
            validation_correct.append(max(scores, key=scores.get) == identity)
        validation_accuracy = float(np.mean(validation_correct))
        print(f"epoch {epoch + 1}/{args.epochs} triplet_loss={mean_loss:.5f} known_identity_val_top1={validation_accuracy:.4f}")
        if validation_accuracy > best_accuracy or (validation_accuracy == best_accuracy and mean_loss < best_loss):
            best_accuracy, best_loss = validation_accuracy, mean_loss
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    if best_state is None:
        raise RuntimeError("Training produced no checkpoint.")
    model.load_state_dict(best_state); model.eval().to(device)
    train_gallery = defaultdict(list)
    train_vectors = embed_records(model, training, args.image_size, device)
    validation_vectors = embed_records(model, validation, args.image_size, device)
    for (_, identity), vector in zip(training, train_vectors):
        train_gallery[identity].append(vector)
    validation_queries = list(zip(validation_vectors, [identity for _, identity in validation]))
    prototypes = {}
    for identity, vectors in train_gallery.items():
        center = np.mean(vectors, axis=0); prototypes[identity] = center / max(float(np.linalg.norm(center)), 1e-12)
    similarities, impostor_scores, correct = [], [], []
    for vector, identity in validation_queries:
        vector = vector / max(float(np.linalg.norm(vector)), 1e-12)
        scores = {key: float(vector @ center) for key, center in prototypes.items()}
        similarities.append(scores[identity])
        impostor_scores.append(max(score for key, score in scores.items() if key != identity))
        correct.append(max(scores, key=scores.get) == identity)
    verified_threshold = float(np.clip(np.quantile(impostor_scores, .995), -1.0, 1.0))
    review_threshold = float(np.clip(np.quantile(impostor_scores, .95), -1.0, verified_threshold))
    state_digest = sha256()
    for key, value in sorted(best_state.items()):
        state_digest.update(key.encode())
        state_digest.update(value.numpy().tobytes())
    model_version = state_digest.hexdigest()[:16]
    args.output.mkdir(parents=True, exist_ok=True)
    checkpoint = {"format": "muzzleid-embedding-v1", "model_version": model_version, "architecture": "resnet50-linear-512-l2", "image_size": args.image_size, "state_dict": best_state}
    checkpoint_path = args.output / "best_model.pt"
    with tempfile.NamedTemporaryFile(dir=args.output, suffix=".tmp", delete=False) as handle:
        temporary_checkpoint = Path(handle.name)
    try:
        torch.save(checkpoint, temporary_checkpoint)
        temporary_checkpoint.replace(checkpoint_path)
    finally:
        temporary_checkpoint.unlink(missing_ok=True)
    calibration = {
        "format": "muzzleid-calibration-v1", "status": "prototype-calibrated", "model_version": model_version,
        "verified_threshold": verified_threshold, "review_threshold": review_threshold,
        "calibration_method": "validation impostor top-identity score quantiles (95%, 99.5%)",
        "validation_images": len(validation_queries), "training_images": len(training), "training_identities": len(identities),
        "known_identity_top1_accuracy": float(np.mean(correct)), "model_selection_known_identity_top1": best_accuracy, "genuine_similarity_median": float(np.median(similarities)),
        "impostor_best_score_p995": float(np.quantile(impostor_scores, .995)),
        "limitations": ["Full images are embedded; no muzzle detector or crop is available.", "Calibration uses a held-out image split of training identities, not unseen animal identities.", "Dataset test identities were excluded because their mapping to training identities is unknown.", "This prototype is not validated for ownership, insurance, fraud, or other consequential decisions."],
    }
    calibration_path = args.output / "calibration.json"
    temporary_calibration = calibration_path.with_suffix(".json.tmp")
    temporary_calibration.write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    temporary_calibration.replace(calibration_path)
    print(json.dumps({"checkpoint": str(checkpoint_path), "calibration": str(calibration_path), **{k: v for k, v in calibration.items() if k in {"model_version", "known_identity_top1_accuracy", "verified_threshold", "review_threshold", "validation_images"}}}, indent=2))


if __name__ == "__main__":
    main()
