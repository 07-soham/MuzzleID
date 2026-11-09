# MuzzleID retrieval scalability

**Run date:** 2026-11-09  
**Runtime:** Windows project virtual environment, FAISS CPU, one FAISS/OpenMP thread.  
**Method:** Synthetic normalized random 512-dimensional vectors; HNSW M=32, efConstruction=160, efSearch=96; 20 random queries; top-3 recall measured against exact `IndexFlatIP` over the identical vectors. Synthetic vectors test index mechanics only and do not represent cattle embeddings or real candidate distributions.

## Results

| Reference vectors | HNSW build | Mean query time (20 queries) | Exact top-3 vector recall | Raw vector storage |
|---:|---:|---:|---:|---:|
| 1,000 | 0.091 s | 0.170 ms | 100.00% | 2.0 MiB |
| 10,000 | 5.613 s | 0.799 ms | 93.33% | 19.5 MiB |

Measurements came from `scripts/benchmark_hnsw.py` in the project `.venv`, with `efSearch=256`. An earlier 10k run at `efSearch=96` measured 83.33% recall and 0.331 ms/query. This recall sensitivity motivated raising the serving value to 256. The benchmark did not finish the 100k build within the local run window, so 100k performance remains unmeasured. These values do not include encrypted persistence, image inference, database access, or animal-level aggregation.

The image index stores one vector per reference image and aggregates retrieved references to distinct animal IDs. Registration updates the process cache incrementally; the database remains authoritative and startup rebuilds a dirty index. Farmer queries remain owner-scoped and currently scan/decrypt their own references per query.

## Interpretation limits

Exact top-3 neighbor recall on random vectors is not biometric accuracy, false-accept rate, or proof that HNSW will retrieve the correct animal. Application latency also includes image decode, neural embedding inference, database/image metadata access, and encryption. A production benchmark needs representative galleries at 1k/10k/100k animals, realistic references per animal, validated cattle query embeddings, concurrent API load, cold/warm startup, and memory/latency percentiles. The 100k target is not yet established for this prototype.
