# Examples

Real output from our bench, unedited. Same batch, same airflow, same 15-minute
parallel screen — so the °C/W figures in them are directly comparable.

| File | What it shows |
|---|---|
| `report-PASS.md` | A card that passed. This is the document a buyer receives. |
| `report-FAIL.md` | A card that failed on memory miscompares — note its thermals are *fine*, which is exactly why a thermal-only check is not enough. |
| `FLEET.csv` | The batch record: one row per run, the input to `--rollup`. |
| `bandwidth-1322421002084.json` | `hx-bandwidth.py --json` on an unlocked card: 1650 GB/s, 93 % of the 1769 GB/s its own bus width and memory clock imply. Taken with an inference server holding 58.9 GB, so it is a pessimistic reading. |

## Reading the rollup

```console
$ bash bin/hx-vram-screen.sh --rollup
```

Two things in `FLEET.csv` are worth pointing out because they are the method
working rather than the method misbehaving:

- **Card `1322321007020` appears twice, failing both times.** A single FAIL is a
  claim; a reproduced FAIL is a fact. Failures get re-run before they are acted
  on.
- **Card `1322421000123` appears as FAIL, then INCONCLUSIVE, then PASS.** That
  is not a flaky card — it is the bench being commissioned. Both early rows are
  ~1-minute runs (36 s of coverage against 848 s for a real screen), and neither
  memory-test log contains a single miscompare. The `FAIL` was the verdict bug
  described in [methodology §8](../docs/METHODOLOGY.md#8-multi-card-benches): a
  worker process does not print the parent's closing `testing PASSed` line, so a
  clean run read as a failure until the verdict was re-derived from error
  markers plus exit code. The rows are left in deliberately — a batch record you
  have quietly edited is not a record.

The 2026-08-26 rows also show the parallel-vs-solo effect in
[§6](../docs/METHODOLOGY.md#6-the-batch-is-the-reference) — the `151327` pair ran
simultaneously and both sit hotter than the same cards run alone.
