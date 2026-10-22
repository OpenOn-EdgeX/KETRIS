# Seven-workload fixed-work experiment — 2026-10-22

This directory archives a measured GPU execution experiment. KETRIS did not demonstrate a throughput improvement over native NVIDIA MPS for this workload. Higher sampled GPU activity accompanied a longer time to finish the same work.

| Evaluation arm | Runs | Mean makespan (s) | Time-weighted GPU busy (%) | Throughput index (native MPS = 100) |
| --- | ---: | ---: | ---: | ---: |
| Native NVIDIA MPS, 100% cap | 5 | 13.2788 | 80.1594 | 100.0000 |
| NVIDIA MPS, 50% cap | 5 | 13.4230 | 81.2571 | 98.9253 |
| KETRIS, 50% cap + phase75 credit policy | 5 | 13.5765 | 83.3205 | 97.8074 |

The KETRIS relative GPU activity index is **103.9435%** (`83.32047924559663 / 80.15941027334287 * 100`). Its throughput index is **97.8074%** (`13.2787898584 / 13.576469189 * 100` for identical work). The former measures relative sampled device activity; it does not measure throughput or useful-work efficiency. The GPU busy percentages above are weighted by observed time across the five evaluation runs, rather than an unweighted average of their percentages. See [summary.csv](summary.csv) and [RESULTS.txt](RESULTS.txt) for the recorded values and limitations.

## Protocol and scope

- Seven concurrent model processes ran inside one experiment Pod on one NVIDIA RTX PRO 6000 Blackwell Server Edition GPU, with driver 590.48.01 and MIG disabled.
- The model was Qwen2.5-0.5B-Instruct with BF16, eager execution, and batch size 1.
- W0–W2 each completed eight requests with 1,024 input tokens and 16 output tokens. W3–W6 each completed eight requests with 128 input tokens and 128 output tokens. Each run therefore completed 56 requests and generated 4,480 tokens, with fixed output lengths.
- Twenty-one runs are archived: six pilot runs followed by fifteen evaluation/holdout runs, five per arm. Pilot selection froze the 50% spatial cap and phase75 credit policy before evaluation. Pilot measurements are excluded from the reported evaluation means.
- All seven workers shared a common release after model loading and warmup. Makespan ends at the latest completion among the seven workers, including the drain period. Workers do not remain active for the entire measurement window.
- The spatial cap is an MPS client limit, not an exclusive partition of GPU cores. The KETRIS policy supplies per-process elapsed-time credits, with prefill supply reduced to 75% while another worker decodes. Decode and idle supply are 100%; credit starts at zero, the bank is capped at 500,000 microseconds, and debt is retained. Credit accounting and gate events alone do not establish sustained phase-specific execution suppression.

This is a fixed-work batch comparison of the GPU execution path, with five short evaluation runs per arm on one device/model/workload combination. It is not a sustained seven-worker saturated service or a Kubernetes queue/admission/scheduler end-to-end benchmark. The order alternated and was not randomized. Neither these observations nor their exploratory intervals establish general superiority or inferiority. See [PROTOCOL.json](PROTOCOL.json), [frozen.json](frozen.json), and [interpretation.json](interpretation.json).

## Evidence and integrity

The original source bundle contained 941 files. This archive preserves every one of the **922 files listed in the original [SHA256SUMS.txt](SHA256SUMS.txt)**, plus that manifest itself, byte for byte. The original manifest SHA-256 is:

```text
39453f052934a2bcf779113d6df929d10a5cad630364d399e54006c3c1e92bbf
```

The 922-entry manifest is historical evidence and has not been regenerated. It covers the original measured artifacts, including raw run data, recorded reports, execution scripts, and the exact archived `libbless.so`. It does not cover this README, `PACKAGING.json`, `runtime-source/`, `runtime-source-provenance.json`, or `verification/`; those are packaging additions. [PACKAGING.json](PACKAGING.json) records the copy inventory and hashes of the added runtime evidence. [ADDED-SHA256SUMS.txt](ADDED-SHA256SUMS.txt) separately covers the added experiment files, except itself.

Eight unlisted bytecode/cache files were omitted. Five unlisted synthetic-example files were copied to [examples/synthetic-console](../../examples/synthetic-console/README.md). Five other unlisted console/public-threshold report files were omitted. No file covered by the original manifest was omitted or edited. Original absolute paths, host identifiers, and the original experiment-time scope text remain as recorded; copying these artifacts does not execute their scripts.

| Path | Contents |
| --- | --- |
| `runs/` | All 21 run directories, with per-worker results and original execution/telemetry logs |
| `PROTOCOL.json`, `frozen-manifest.json`, `frozen.json` | Workload, selection rules, and frozen evaluation plan |
| `run-start.json`, `environment.json` | Recorded execution-source hashes and environment for the 2026-10-22 experiment |
| `results.csv`, `summary.csv`, `analysis.json` | Per-run and evaluation summary data |
| `runtime-audit.json`, `interpretation.json` | Raw runtime/accounting audit and bounded interpretation |
| `RESULTS.txt`, `comparison.png` | Original human-readable report and plot |
| `runtime-source/`, `runtime-source-provenance.json` | The archived source and historical provenance for the measured runtime binary |
| `verification/` | Fresh offline verification logs and machine-readable outcomes created during packaging |

## Archived runtime provenance

The included `libbless.so` is the exact 87,424-byte runtime recorded by this experiment. Its SHA-256 is:

```text
65a69816dd4e1fb98140c414b508365ac8c1ef137484e2ccafb2f304b55f289d
```

The six files in [runtime-source/](runtime-source/) were copied unchanged from the preceding 2026-10-21 `client-credit/runtime` archive. Every source hash matches that archive's unchanged [runtime-source-provenance.json](runtime-source-provenance.json), whose binary hash also matches this experiment's `libbless.so`. The provenance retains the historical build command and its experiment-specific `KETRIS_EXPERIMENT_MAX_CREDIT_CAP_US=4000000` define. Its `runtime_binary_source` and `remote_executed_sources` fields refer to the earlier client-credit experiment; the seven-workload execution scripts are evidenced separately by this directory's `run-start.json`, `environment.json`, and runtime audit.

The repository's current development source at [runtime/libbless](../../runtime/libbless/) differs from this archived experiment source. Building that current source is not a reproduction of the measured binary. The small binary is retained as exact historical evidence; packaging and offline analysis do not load or deploy it. A fresh build can also differ because of compiler/toolchain details.

## Verify and reanalyze offline

From the repository root, verify the original evidence and run all 39 tests:

```bash
cd experiments/seven-workload-20261022
sha256sum --check SHA256SUMS.txt
sha256sum --check ADDED-SHA256SUMS.txt
python3 -B analyze.py --self-test
python3 -B audit_runtime.py --self-test
python3 -B interpret_results.py --self-test
python3 -B -m unittest -v test_policy
```

The analysis, runtime audit, and interpretation suites contain 16, 14, and 4 tests respectively: 34 combined. The policy suite contains another 5. These commands use Python's standard library and do not require a GPU. The fresh captured outputs are in [verification/](verification/).

The full analysis commands write derived files. Preserve the archive by running them on a new disposable copy. From the experiment directory:

```bash
artifact="$PWD"
analysis_copy="$(mktemp -d)"
cp -a "$artifact/." "$analysis_copy/"
python3 -B "$analysis_copy/analyze.py" "$analysis_copy" --no-plot
python3 -B "$analysis_copy/audit_runtime.py" "$analysis_copy"
python3 -B "$analysis_copy/interpret_results.py" "$analysis_copy"
printf 'Regenerated outputs: %s\n' "$analysis_copy"
```

The `--no-plot` option keeps reanalysis free of optional plotting dependencies. To regenerate the plot as well, install Matplotlib in the analysis environment and omit `--no-plot`. Absolute root/path fields in regenerated JSON reflect the new location, so regenerated reports need not have the original hashes. Packaging verification confirmed that all 21 runs pass, the 15 evaluation runs remain complete, and regenerated `results.csv` and `summary.csv` match the archive byte for byte.

The original `run_study.py`, `worker.py`, `host_watch.py`, and `postflight.py` contain fixed-host, namespace, process-ID, GPU, and filesystem assumptions. They are archived research execution scripts, not a portable automatic rerun or deployment entry point. In particular, the watcher and postflight scripts access the host/cluster and have top-level side effects if invoked or imported. None is invoked by the offline commands above. Repeating the live experiment requires a separately reviewed environment and explicit adaptation of those assumptions.
