# Collection finished 2026-09-25

All nine categories reached their target, so `api_top.yml` was moved out of
`.github/workflows/`.

It was still firing daily on schedule, finding nothing left to do, and raising
a fresh "collection finished" issue every time - two arrived before it was
stopped. Each empty run also spent a dozen requests for nothing.

Final dataset: `merged/cults3d_merged.csv`, 338,075 unique models.

To collect more later (a deeper `--per-category`, or a different date range),
move this file back and raise the target. Nothing else needs changing; progress
is tracked per category in `top_data/progress.json`.
