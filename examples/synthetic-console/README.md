# Synthetic console illustration

The displayed **67.1% is synthetic and is not a measured KETRIS result**. It is the arithmetic mean of the five hard-coded illustration values 65.8, 67.4, 66.9, 68.2, and 67.2. The script does not read experiment data. These numbers are relative GPU activity indices, not absolute GPU busy percentages, throughput measurements, or evidence that an evaluation target was achieved.

Run from the repository root:

```bash
python3 -B examples/synthetic-console/print_synthetic_example.py
python3 -B examples/synthetic-console/print_synthetic_example.py --summary
```

`print_display_example.py` is the original compatibility wrapper. `synthetic_example.txt` and `display_example.txt` contain the same complete example; `display_example_summary.txt` contains its summary. All five files were copied unchanged from files outside the measured bundle's original hash manifest. [verification.json](verification.json) records output comparisons and hashes of the five copied files.

The measured experiment is kept in [experiments/seven-workload-20261022](../../experiments/seven-workload-20261022/README.md). There, native MPS averaged 80.1594% GPU busy and 13.2788 seconds; KETRIS averaged 83.3205% and 13.5765 seconds. Its measured relative GPU activity index was 103.9435%, and its throughput index was 97.8074% with native MPS = 100. Those observations do not show a throughput improvement for this workload.
