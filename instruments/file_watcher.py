"""
instruments/file_watcher.py

Importable file watcher: monitors a directory for new instrument CSV
files, parses + QC's each one, and appends a summary to a master results
CSV. This is Week 2 Day 9's logic, refactored from a standalone script
into a reusable class — the same way you'd structure it for production.

USES PollingObserver BY DEFAULT — see Week 2 notes: InotifyObserver
silently detects nothing on WSL2 /mnt/c paths (DrvFs filesystem) and on
some network/Docker mounts. PollingObserver works everywhere via
os.scandir(), at the cost of up to `poll_timeout` seconds of latency.

USAGE:
    watcher = ResultsWatcher(
        incoming_dir="data/raw",
        master_csv="data/processed/master_results.csv",
    )
    watcher.start()
    ... instrument writes files to data/raw/ ...
    watcher.stop()
"""

import time
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from instruments.qc_utils import compute_well_qc, plate_summary

logger = logging.getLogger("results_watcher")


class _Handler(FileSystemEventHandler):
    """Internal watchdog handler — delegates to ResultsWatcher.process_file()."""

    def __init__(self, watcher: "ResultsWatcher"):
        super().__init__()
        self.watcher = watcher

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith(".csv"):
            return
        time.sleep(0.05)   # ensure write completes before reading
        self.watcher.process_file(Path(event.src_path))


class ResultsWatcher:
    """
    Watches `incoming_dir` for new .csv files. Each file is parsed,
    QC-scored, and appended as a summary row to `master_csv`.

    Attributes
    ----------
    processed_count : int
        Number of files successfully ingested since start().
    processed_files : list[str]
        Filenames that have been ingested (useful for tests/demos).
    """

    def __init__(
        self,
        incoming_dir: str,
        master_csv: str,
        cv_threshold: float = 15.0,
        poll_timeout: float = 0.5,
    ):
        self.incoming_dir = Path(incoming_dir)
        self.master_csv   = Path(master_csv)
        self.cv_threshold = cv_threshold
        self.poll_timeout = poll_timeout

        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.master_csv.parent.mkdir(parents=True, exist_ok=True)

        self.processed_count = 0
        self.processed_files = []
        self._observer = None

    def start(self):
        self._observer = PollingObserver(timeout=self.poll_timeout)
        self._observer.schedule(_Handler(self), str(self.incoming_dir), recursive=False)
        self._observer.start()
        logger.info(f"ResultsWatcher started on {self.incoming_dir} "
                    f"(PollingObserver, timeout={self.poll_timeout}s)")

    def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer.join()
            logger.info(f"ResultsWatcher stopped. Total ingested: {self.processed_count}")

    def process_file(self, filepath: Path):
        """Parse a single result CSV, QC it, and append to master_csv."""
        try:
            df = pd.read_csv(filepath, comment="#", index_col=0)
            df.columns = [int(c) for c in df.columns]

            plate_id = filepath.stem.replace("plate_", "")

            qc = compute_well_qc(df, cv_threshold=self.cv_threshold)
            summary = plate_summary(qc)

            record = {
                "plate_id": plate_id,
                "ingested_at": datetime.now().isoformat(timespec="seconds"),
                "source_file": filepath.name,
                "n_pass": summary["n_pass"],
                "n_fail": summary["n_fail"],
                "mean_cv_pct": summary["mean_cv_pct"],
                "total_outliers": summary["total_outliers"],
                "overall_status": summary["overall_status"],
            }

            record_df = pd.DataFrame([record])
            if self.master_csv.exists():
                record_df.to_csv(self.master_csv, mode="a", header=False, index=False)
            else:
                record_df.to_csv(self.master_csv, mode="w", header=True, index=False)

            self.processed_count += 1
            self.processed_files.append(filepath.name)

            logger.info(
                f"Ingested {plate_id}: {summary['n_pass']}/{summary['n_columns']} PASS, "
                f"overall={summary['overall_status']} "
                f"(total: {self.processed_count})"
            )

        except Exception as e:
            logger.error(f"Failed to process {filepath.name}: {e}")

    def wait_for_count(self, target_count: int, timeout: float = 10.0) -> bool:
        """
        Block until `processed_count >= target_count` or timeout.
        Useful in scripts/tests that need to know when all expected
        files have been ingested.
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.processed_count >= target_count:
                return True
            time.sleep(0.1)
        return False
