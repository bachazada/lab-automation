"""
run_pipeline.py

THE FULL END-TO-END PIPELINE.

One command runs the entire lab automation suite:

    1. Load config.yaml
    2. Preview the protocol (what the robot WOULD do)
    3. Start the ResultsWatcher (Week 2 file watching)
    4. Generate N plate measurements via MockPlateReader (simulating the
       robot + instrument completing N runs)
    5. Each measurement is written as a CSV -> watcher ingests it
       automatically -> QC'd -> appended to master_results.csv
    6. Print a final summary table with quality ranking and drift detection
    7. Print the command to launch the dashboard

HOW TO RUN:
    python3 run_pipeline.py
    python3 run_pipeline.py --config config.yaml
    python3 run_pipeline.py --preview-only      # just show the protocol plan, don't run

This is the file that demonstrates: Week 1 (protocol design) + Week 2
(timing, file watching, consolidation) + Week 3 (config-driven, multi-protocol,
tested) all working together as ONE system.
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from protocols.protocol_generator import load_config, preview as protocol_preview
from instruments.mock_plate_reader import MockPlateReader
from instruments.file_watcher import ResultsWatcher
from instruments.qc_utils import compute_well_qc, plate_summary


def run_pipeline(config_path: str = None, preview_only: bool = False):
    config = load_config(config_path)

    logger.info("=" * 65)
    logger.info(f"LAB AUTOMATION SUITE — {config['protocol']['name']}")
    logger.info("=" * 65)

    # ── STEP 1: PROTOCOL PREVIEW ────────────────────────────────────────────
    logger.info("")
    logger.info("STEP 1: Protocol Preview")
    logger.info("-" * 65)
    protocol_preview(config_path)

    if preview_only:
        logger.info("\n--preview-only set — stopping here. No measurements run.")
        return

    # ── STEP 2: SETUP DIRECTORIES ────────────────────────────────────────────
    pipe_cfg = config["pipeline"]
    incoming_dir = PROJECT_ROOT / pipe_cfg["incoming_dir"]
    master_csv   = PROJECT_ROOT / pipe_cfg["master_results_csv"]

    # Clean slate for a fresh demo run
    for f in incoming_dir.glob("plate_*.csv"):
        f.unlink()
    if master_csv.exists():
        master_csv.unlink()

    # ── STEP 3: START THE WATCHER ────────────────────────────────────────────
    logger.info("")
    logger.info("STEP 2: Starting ResultsWatcher")
    logger.info("-" * 65)

    watcher = ResultsWatcher(
        incoming_dir=str(incoming_dir),
        master_csv=str(master_csv),
        cv_threshold=config["validation"]["max_cv_threshold"],
        poll_timeout=0.5,
    )
    watcher.start()

    # ── STEP 4: RUN MEASUREMENTS ──────────────────────────────────────────────
    logger.info("")
    logger.info("STEP 3: Running Measurements (simulating robot + instrument)")
    logger.info("-" * 65)

    instr_cfg = config["instrument"]
    reader = MockPlateReader(
        noise_cv=instr_cfg["noise_cv"],
        outlier_prob=instr_cfg["outlier_prob"],
    )
    reader.connect()

    num_runs = pipe_cfg["num_runs"]
    delay    = pipe_cfg["seconds_between_runs"]

    for i in range(1, num_runs + 1):
        plate_id = f"RUN_{i:03d}"

        # Slight run-to-run variation, like real instrument drift
        start_conc = 2.0 + (i % 3) * 0.1
        result = reader.run_measurement(plate_id=plate_id, serial_dilution_start=start_conc)

        out_path = incoming_dir / f"plate_{plate_id}.csv"
        with open(out_path, "w") as f:
            f.write(f"# PLATE_ID={plate_id}\n")
            f.write(f"# TIMESTAMP={result.timestamp}\n")
            f.write(result.well_data.to_csv())

        logger.info(f"  [ROBOT+INSTRUMENT] {plate_id} measured and written to {out_path.name}")
        time.sleep(delay)

    reader.disconnect()

    # ── STEP 5: WAIT FOR INGESTION ───────────────────────────────────────────
    logger.info("")
    logger.info("STEP 4: Waiting for ResultsWatcher to finish ingesting")
    logger.info("-" * 65)

    ok = watcher.wait_for_count(num_runs, timeout=15.0)
    watcher.stop()

    if not ok:
        logger.warning(f"Only {watcher.processed_count}/{num_runs} files were ingested "
                       f"within timeout!")
    else:
        logger.info(f"All {num_runs} files ingested successfully.")

    # ── STEP 6: FINAL SUMMARY ─────────────────────────────────────────────────
    logger.info("")
    logger.info("STEP 5: Final Summary")
    logger.info("-" * 65)

    if not master_csv.exists():
        logger.error("master_results.csv was not created — pipeline failed.")
        return

    df = pd.read_csv(master_csv)
    df["pass_rate"]       = (df["n_pass"] / (df["n_pass"] + df["n_fail"])).round(3)
    df["rolling_mean_cv"] = df["mean_cv_pct"].rolling(window=3, min_periods=1).mean().round(1)

    # quality_rank: primary sort = pass_rate (desc), tiebreaker = mean_cv_pct (asc).
    # A simple .rank() on pass_rate alone breaks ties by row order, which can
    # make a high-CV%-among-equals run look "best" purely by being first.
    rank_order = df.sort_values(["pass_rate", "mean_cv_pct"], ascending=[False, True])
    rank_map = {plate_id: rank for rank, plate_id in enumerate(rank_order["plate_id"], start=1)}
    df["quality_rank"] = df["plate_id"].map(rank_map)

    print("\n" + df.to_string(index=False))

    n_pass = (df["overall_status"] == "PASS").sum()
    n_fail = (df["overall_status"] == "FAIL").sum()

    print(f"\n{'='*65}")
    print(f"PIPELINE COMPLETE")
    print(f"  Total runs:     {len(df)}")
    print(f"  Overall PASS:   {n_pass}")
    print(f"  Overall FAIL:   {n_fail}")
    print(f"  Mean CV%:       {df['mean_cv_pct'].mean():.1f}%")

    best  = df.loc[df["quality_rank"].idxmin()]
    worst = df.loc[df["quality_rank"].idxmax()]
    print(f"  Best run:       {best['plate_id']} (CV%={best['mean_cv_pct']})")
    print(f"  Worst run:      {worst['plate_id']} (CV%={worst['mean_cv_pct']})")

    trend = df["rolling_mean_cv"].iloc[-1] - df["rolling_mean_cv"].iloc[0]
    trend_str = "INCREASING (investigate drift)" if trend > 1 else \
                "DECREASING (improving)" if trend < -1 else "STABLE"
    print(f"  CV% trend:      {trend_str}")
    print(f"{'='*65}")

    print(f"\nTo view the dashboard:")
    print(f"  streamlit run dashboard/app.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lab Automation Suite — full pipeline")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--preview-only", action="store_true",
                       help="Show the protocol plan without running measurements")
    args = parser.parse_args()

    run_pipeline(config_path=args.config, preview_only=args.preview_only)
