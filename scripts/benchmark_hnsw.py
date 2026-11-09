"""Synthetic HNSW mechanics benchmark; not a biometric accuracy test."""
import time

import faiss
import numpy as np


def main():
    faiss.omp_set_num_threads(1)
    rng = np.random.default_rng(20261109)
    queries = rng.normal(size=(20, 512)).astype(np.float32)
    queries /= np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
    for count in (1_000, 10_000):
        vectors = rng.normal(size=(count, 512)).astype(np.float32)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        index = faiss.IndexHNSWFlat(512, 32, faiss.METRIC_INNER_PRODUCT)
        index.hnsw.efConstruction = 160
        index.hnsw.efSearch = 256
        start = time.perf_counter()
        index.add(vectors)
        build_seconds = time.perf_counter() - start
        start = time.perf_counter()
        _, approximate = index.search(queries, 3)
        query_ms = (time.perf_counter() - start) * 1000 / len(queries)
        exact = faiss.IndexFlatIP(512)
        exact.add(vectors)
        _, reference = exact.search(queries, 3)
        recall = sum(len(set(approximate[i]) & set(reference[i])) for i in range(len(queries))) / (len(queries) * 3)
        print({"vectors": count, "build_seconds": round(build_seconds, 3), "query_ms_mean": round(query_ms, 3), "exact_top3_recall": round(recall, 4), "vector_mib": round(vectors.nbytes / 1024**2, 1)}, flush=True)


if __name__ == "__main__":
    main()
