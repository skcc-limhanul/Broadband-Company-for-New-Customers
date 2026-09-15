# Broadband Company for New Customers

This project imports broadband promotion and daily new-subscriber data into
SQLite, then finds similar historical subscriber patterns within each service,
service subtype, and sales channel. The current analysis reads
`subscribers_historical`; the importer also stores promotion and new-subscriber
records for separate use.

## Project tasks

| Task | Entry point |
| --- | --- |
| Set up Python dependencies | [Setup](#setup) |
| Import source workbooks into SQLite | [Prepare the database](#prepare-the-database) |
| Run the complete segment experiment | [Daily subscriber similarity by segment](#daily-subscriber-similarity-by-segment) |
| Try a smaller configuration grid | [Quick experiment](#quick-experiment) |
| Inspect generated results and analysis reports | [Results and reports](#results-and-reports) |
| Run regression checks | [Validation](#validation) |

## Setup

Run commands from the repository root. Use Python 3.10 or newer and an isolated
virtual environment outside the repository:

```bash
python3 -m venv ~/.venvs/broadband-company
source ~/.venvs/broadband-company/bin/activate
python -m pip install pandas numpy scipy matplotlib openpyxl
```

For the comprehensive experiment, install PyTorch. Numba is optional and speeds
up dynamic time warping (DTW) distance calculations:

```bash
python -m pip install torch
python -m pip install numba
```

There is currently no pinned dependency manifest. `sqlite3` and `unittest` are
included with Python.

## Prepare the database

The analysis expects `database/broadband_company.db` with a
`subscribers_historical` table, even when displaying CLI help or importing the
module for tests. If using the existing database, skip the import task.

To rebuild from source workbooks, provide these local inputs:

| Workbook | Sheet | Destination table |
| --- | --- | --- |
| `dataset/broadband_company_subscribers.xlsx` | `프로모션정책` | `promotion` |
| `dataset/broadband_company_subscribers.xlsx` | `개통자수_일별` | `subscribers_historical` |
| `dataset/subscribers_new.xlsx` | First sheet | `subscribers_new` |

The importer renames Korean source columns to English database columns and drops
workbook-only weighting columns from subscriber data. Source columns must match
[the importer](process/dataset/create_database.py).

**Running this command replaces all three destination tables.** Preserve any
local data you need before rebuilding:

```bash
python process/dataset/create_database.py
```

Keep private source workbooks and generated local artifacts out of version
control.

## Daily subscriber similarity by segment

The analysis unit is **one `(service, service_sub, channel)` combination**.
Each target is compared only with older windows from that same combination.
Subscriber counts from different segments are never added together.

Run all 20 segments independently with the complete experimental protocol:

```bash
python process/similarity/similarity_check.py --comprehensive --workers 6 \
  --output-dir /tmp/broadband-leaf-analysis
```

The comprehensive preset requires PyTorch; optional Numba accelerates DTW.
It tests 700 configurations plus six weekday-aware DTW refinements **per
segment**, using five window lengths (30, 45, 60, 75, 90 days), eleven transforms,
statistical methods, hybrids, and two CNN architectures with two seeds.
Selection uses each segment's earlier tuning dates, with later holdout dates
reported separately. There is no single winner inherited from total subscribers.

To analyze one leaf segment:

```bash
python process/similarity/similarity_check.py --comprehensive \
  --service '초고속' --service-sub IP --channel 직접 \
  --output-dir /tmp/broadband-leaf-single
```

A partial filter, such as `--service '초고속'`, runs each of the ten matching
leaf segments separately. With no filters, all 20 are analyzed separately.
`--workers` controls the number of independent segment processes (default 1).
The Python helper `select_daily_series` rejects inputs containing multiple
segment keys; it cannot silently create a total-subscriber series. Duplicate
or missing daily observations within a segment require correction before use.

## Quick experiment

For a smaller experiment, omit `--comprehensive` and choose a configuration grid:

```bash
python process/similarity/similarity_check.py \
  --service '초고속' --service-sub IP --channel 직접 \
  --window-length 45 --methods correlation --preprocessors smooth3_zscore \
  --target-end 2027-02-18 --top-n 5 \
  --output-dir /tmp/broadband-leaf-quick
```

The quick sweep retains the earlier evaluation proxy. Compare its scores only
within that protocol; the reports use the comprehensive chronological protocol.
The comprehensive preset accepts segment filters, `--target-end`, `--top-n`,
`--workers` and `--output-dir`. Its fixed grid and plots are not changed by the
quick-sweep `--methods`, `--preprocessors`, `--window-length`, `--query-count`
or `--no-plots` options.

## Results and reports

Results default to `/tmp/broadband-segment-similarity-results`; the examples above
use their own directories outside the repository:

- `segment_manifest.json` maps stable segment directory IDs to full dimension
  keys and records each segment's configuration and data fingerprint.
- `segment_selected_configurations.csv` lists the selected setting and scores
  separately for each segment.
- `segment_top_matches.csv` lists each segment's top historical matches.
- Each segment directory contains its full comparison CSV, per-query scores,
  all configurations' latest matches, metadata, and aligned plots.
- Every CSV includes `segment_id`, `service`, `service_sub` and `channel`.

| Report | English | Korean |
| --- | --- | --- |
| Complete analysis | [Report](document/similarity-analysis-report.md) | [한국어](document/similarity-analysis-report_ko.md) |
| Beginner's guide | [Guide](document/similarity-analysis-report-simple.md) | [한국어](document/similarity-analysis-report-simple_ko.md) |

The [segment configuration appendix](document/similarity-analysis-segment-configurations.md)
contains the detailed configuration comparison. Reports describe the recorded
experiment; running the CLI writes new result artifacts without rewriting them.

Earlier total-subscriber results are superseded and must not be used as evidence
for leaf-segment similarity.

## Validation

With the database and dependencies available, run the regression checks with:

```bash
python -m unittest discover -s process/similarity -p 'test_*.py'
```
