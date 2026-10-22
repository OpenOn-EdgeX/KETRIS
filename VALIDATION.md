# Initial publication validation — 2026-10-22

These checks were run against this publication checkout. They validate packaging, offline logic, builds, and archived measurement consistency. No new GPU/NPU experiment or Kubernetes deployment was performed for this publication.

| Check | Fresh result |
|---|---|
| MIG sharing Python unit tests | 70 passed |
| Institution bridge Python unit tests | 66 passed |
| Dashboard portable Python tests: API, normalization, installation, observer | 27 passed |
| Main device-plugin Go suite | Passed; 56 passing test/subtest events |
| MIG device-plugin Go suite | Passed; 9 passing test/subtest events |
| Frontend Vitest | 20 tests passed in 3 files |
| Frontend production build | TypeScript check and Vite build passed |
| Native CPU checks with UBSan | `time_bootstrap_test`, `time_refill_test`, `qmd_layout_test` passed |
| CUDA 12.9 source builds | `libbless.so`, `gating_lib.so`, `kernel_tracer.so` built successfully |
| Experiment analyzer self-tests | 16 passed |
| Experiment runtime auditor self-tests | 14 passed |
| Experiment interpretation self-tests | 4 passed |
| Experiment policy tests | 5 passed |
| Fresh offline measurement reanalysis | 21 runs validated; `results.csv` and `summary.csv` byte-identical to archived originals |
| Original experiment SHA256 manifest | All 922 files matched |
| Source snapshot manifest | All 430 copied source files matched |

Python checks used existing dependencies with `PYTHONPATH` directed to this checkout. Go tests used the installed `/usr/local/go/bin/go` with network module retrieval disabled. Frontend dependencies were installed using `npm ci --offline --ignore-scripts`. Local socket tests required execution outside the environment's network sandbox; they did not access or change production services.

Native shared libraries rebuilt during verification are ignored build products. The small binary under `experiments/seven-workload-20261022/libbless.so` is a separate, original archived execution artifact and is intentionally committed with its historical hash.

## Checks intentionally outside this publication scope

- Historical dashboard archive/store tests depend on separately held October 12–13 reports. These two modules were not included in the portable dashboard test selection.
- Full container image builds, cluster installation, GPU/NPU execution, browser E2E, and new performance measurements were not rerun.
- Deployment scripts retain environment-specific paths, image tags, and hardware assumptions. Successful unit tests do not establish deployment portability.
- The existing `gating_lib.so` build emits an ignored-`fread`-return-value compiler warning; compilation still succeeds. This publication does not change runtime behavior to address it.

## Publication checks

The publication excludes historical Git objects, oversized trace archives, model weights, credential-bearing host administration scripts, and generated development binaries. A scoped scan found no private-key, known access-token, embedded URL password, or Kubernetes client-key patterns in the copied files. This scan is not a comprehensive security audit.

The target repository is private. Synthetic console examples have their own directory and explicit provenance; they are not measurement evidence.

`git diff --cached --check` reports existing whitespace in preserved files: 27 archived measurement lines, 2 runtime source lines, and 7 MIG source files with a final blank line. These were retained to keep the source and original-data hashes unchanged; this publication does not claim a whitespace-clean import.
