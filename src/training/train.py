"""Train and evaluate an identity-disjoint cattle embedding prototype.

Only data/train/dataset is read. Animal identities are split into disjoint
training, checkpoint-selection, calibration, and final-test partitions.
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
from src.inference.decision import decide

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


def collect_partitions(dataset: Path, seed: int):
    """Deduplicate exact bytes and split whole identities into four partitions."""
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
    eligible = {identity: paths for identity, paths in groups.items() if len(paths) >= 4}
    identities = sorted(eligible)
    if len(identities) < 40:
        raise ValueError("At least 40 identities with four unique readable images each are required for identity-disjoint training, calibration, and testing.")
    rng = random.Random(seed)
    rng.shuffle(identities)
    count = len(identities)
    train_end = int(count * .70)
    validation_end = int(count * .80)
    calibration_end = int(count * .90)
    identity_splits = {
        "train": identities[:train_end],
        "validation": identities[train_end:validation_end],
        "calibration": identities[validation_end:calibration_end],
        "test": identities[calibration_end:],
    }
    partitions = {}
    for split, split_identities in identity_splits.items():
        partitions[split] = {}
        for identity in split_identities:
            paths = eligible[identity][:]
            rng.shuffle(paths)
            midpoint = len(paths) // 2
            partitions[split][identity] = {
                "gallery": [(path, identity) for path in paths[:midpoint]],
                "query": [(path, identity) for path in paths[midpoint:]],
            }
    return partitions


def flatten(partition, part="gallery"):
    return [record for records in partition.values() for record in records[part]]


def retrieval_metrics(model, known_partition, unknown_partition, image_size, device):
    """Compute enrolled identity accuracy and open-set max-score distributions."""
    gallery_records = flatten(known_partition, "gallery")
    known_records = flatten(known_partition, "query")
    unknown_records = flatten(unknown_partition, "query")
    gallery_vectors = embed_records(model, gallery_records, image_size, device)
    known_vectors = embed_records(model, known_records, image_size, device)
    unknown_vectors = embed_records(model, unknown_records, image_size, device)
    centers = defaultdict(list)
    for (_, identity), vector in zip(gallery_records, gallery_vectors):
        centers[identity].append(vector)
    centers = {identity: np.mean(vectors, axis=0) for identity, vectors in centers.items()}
    centers = {identity: vector / max(float(np.linalg.norm(vector)), 1e-12) for identity, vector in centers.items()}

    def score_rows(records, vectors):
        rows = []
        for (_, identity), vector in zip(records, vectors):
            vector = vector / max(float(np.linalg.norm(vector)), 1e-12)
            scores = {candidate: float(vector @ center) for candidate, center in centers.items()}
            best_identity = max(scores, key=scores.get)
            rows.append({"identity": identity, "best_identity": best_identity, "best_score": scores[best_identity], "genuine_score": scores.get(identity)})
        return rows

    known_scores = score_rows(known_records, known_vectors)
    unknown_scores = score_rows(unknown_records, unknown_vectors)
    return known_scores, unknown_scores


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
    def __init__(self, records, labels, identities_per_batch=16, samples_per_identity=4, max_batches=None):
        self.by_identity = defaultdict(list)
        for index, (_, identity) in enumerate(records):
            self.by_identity[labels[identity]].append(index)
        self.identities_per_batch = min(identities_per_batch, len(self.by_identity))
        self.samples_per_identity = samples_per_identity
        natural_batches = max(1, len(records) // (self.identities_per_batch * samples_per_identity))
        self.batches = min(natural_batches, max_batches) if max_batches is not None else natural_batches

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
    main_identity_disjoint()

def main_identity_disjoint():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "train" / "dataset")
    parser.add_argument("--output", type=Path, default=ROOT / "models")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-identities", type=int, default=16)
    parser.add_argument("--samples-per-identity", type=int, default=4)
    parser.add_argument("--batches-per-epoch", type=int, default=24, help="Bound CPU work per epoch while sampling balanced identity batches.")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--image-size", type=int, default=128)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if args.epochs < 1 or args.batches_per_epoch < 1:
        parser.error("epochs and batches-per-epoch must be positive")
    torch, build_model, triplet_loss = load_stack()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    device = "cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu" if args.device == "auto" else args.device
    if device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA was requested but is unavailable")
    partitions = collect_partitions(args.data, args.seed)
    training = flatten(partitions["train"], "gallery") + flatten(partitions["train"], "query")
    labels = sorted({identity for _, identity in training})
    label_ids = {identity: index for index, identity in enumerate(labels)}
    dataset = ImageDataset(training, label_ids, args.image_size)
    sampler = IdentityBatchSampler(training, label_ids, args.batch_identities, args.samples_per_identity, args.batches_per_epoch)
    loader = torch.utils.data.DataLoader(dataset, batch_sampler=sampler, num_workers=0)
    model = build_model(pretrained=True).to(device)
    for parameter in model.features.parameters():
        parameter.requires_grad = False
    model.features[7].requires_grad_(True)  # fine-tune ResNet-50 layer4 only
    model.embedding.requires_grad_(True)
    optimizer = torch.optim.AdamW((parameter for parameter in model.parameters() if parameter.requires_grad), lr=2e-4, weight_decay=1e-4)
    best_accuracy, best_loss, best_state = -1.0, float("inf"), None
    for epoch in range(args.epochs):
        model.train()
        for module in model.features[:7]:
            module.eval()  # frozen BatchNorm statistics stay at ImageNet values
        losses = []
        for images, batch_labels in loader:
            images, batch_labels = images.to(device), batch_labels.to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = triplet_loss(model(images), batch_labels)
            loss.backward(); optimizer.step()
            losses.append(float(loss.detach().cpu()))
        model.eval()
        validation_scores, _ = retrieval_metrics(model, partitions["validation"], {}, args.image_size, device)
        validation_accuracy = float(np.mean([r["best_identity"] == r["identity"] for r in validation_scores]))
        mean_loss = float(np.mean(losses))
        print(f"epoch {epoch + 1}/{args.epochs} triplet_loss={mean_loss:.5f} identity-disjoint-validation-top1={validation_accuracy:.4f}", flush=True)
        if validation_accuracy > best_accuracy or (validation_accuracy == best_accuracy and mean_loss < best_loss):
            best_accuracy, best_loss = validation_accuracy, mean_loss
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    if best_state is None:
        raise RuntimeError("Training produced no checkpoint.")
    model.load_state_dict(best_state); model.eval().to(device)

    def split_enrolled_unknown(partition):
        identities = list(partition)
        return ({identity: partition[identity] for i, identity in enumerate(identities) if i % 2 == 0},
                {identity: partition[identity] for i, identity in enumerate(identities) if i % 2 == 1})

    calibration_known, calibration_unknown = split_enrolled_unknown(partitions["calibration"])
    test_known, test_unknown = split_enrolled_unknown(partitions["test"])
    _, calibration_unknown_rows = retrieval_metrics(model, calibration_known, calibration_unknown, args.image_size, device)
    test_known_rows, test_unknown_rows = retrieval_metrics(model, test_known, test_unknown, args.image_size, device)
    if not calibration_unknown_rows or not test_known_rows or not test_unknown_rows:
        raise RuntimeError("Identity-disjoint calibration/test partitions must each include enrolled and unknown identities.")
    unknown_scores = np.asarray([row["best_score"] for row in calibration_unknown_rows], dtype=np.float64)
    review_threshold = float(np.quantile(unknown_scores, .95, method="higher"))
    verified_threshold = float(np.quantile(unknown_scores, .995, method="higher"))
    review_threshold = float(np.clip(review_threshold, -1, 1))
    verified_threshold = float(np.clip(max(verified_threshold, review_threshold), -1, 1))
    known_correct = [row["best_identity"] == row["identity"] for row in test_known_rows]

    def decide_score(score):
        return decide(float(score), verified_threshold=verified_threshold, review_threshold=review_threshold)

    known_decisions = [decide_score(row["best_score"]) for row in test_known_rows]
    unknown_decisions = [decide_score(row["best_score"]) for row in test_unknown_rows]
    digest = sha256()
    for key, value in sorted(best_state.items()):
        digest.update(key.encode()); digest.update(value.numpy().tobytes())
    model_version = digest.hexdigest()[:16]
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
        "review_threshold": review_threshold, "verified_threshold": verified_threshold,
        "calibration_method": "95th and 99.5th higher quantiles of identity-disjoint unknown queries' maximum enrolled-gallery cosine scores",
        "training_images": len(training), "training_identities": len(partitions["train"]),
        "validation_identities": len(partitions["validation"]),
        "calibration_enrolled_identities": len(calibration_known), "calibration_unknown_identities": len(calibration_unknown),
        "calibration_unknown_queries": len(calibration_unknown_rows),
        "test_enrolled_identities": len(test_known), "test_unknown_identities": len(test_unknown),
        "test_known_queries": len(test_known_rows), "test_unknown_queries": len(test_unknown_rows),
        "validation_known_identity_top1_accuracy": best_accuracy,
        "test_known_identity_top1_accuracy": float(np.mean(known_correct)),
        "test_known_verified_correct_rate": float(np.mean([ok and decision == "VERIFIED" for ok, decision in zip(known_correct, known_decisions)])),
        "test_known_false_reject_rate": float(np.mean([decision != "VERIFIED" for decision in known_decisions])),
        "test_unknown_false_accept_rate": float(np.mean([decision == "VERIFIED" for decision in unknown_decisions])),
        "test_unknown_review_rate": float(np.mean([decision == "REVIEW" for decision in unknown_decisions])),
        "test_unknown_reject_rate": float(np.mean([decision == "UNKNOWN" for decision in unknown_decisions])),
        "calibration_unknown_max_score_p95": float(np.quantile(unknown_scores, .95, method="higher")),
        "calibration_unknown_max_score_p995": float(np.quantile(unknown_scores, .995, method="higher")),
        "limitations": ["Full images are embedded; no muzzle detector or crop is available.", "Identity partitions are folder-disjoint, but source identity provenance and population representativeness are unverified.", "Metrics are a prototype research result, not evidence for consequential decisions."],
    }
    calibration_path = args.output / "calibration.json"
    temporary_calibration = calibration_path.with_suffix(".json.tmp")
    temporary_calibration.write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    temporary_calibration.replace(calibration_path)
    print(json.dumps({"checkpoint": str(checkpoint_path), "calibration": str(calibration_path), **{key: value for key, value in calibration.items() if key.startswith("test_") or key in {"model_version", "verified_threshold", "review_threshold"}}}, indent=2), flush=True)


if __name__ == "__main__":
    main_identity_disjoint()
