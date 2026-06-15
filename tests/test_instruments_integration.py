"""
tests/test_instruments_integration.py

Day 17 integration test: MockPlateReader writes CSV files -> ResultsWatcher
detects and ingests them -> master_results.csv contains all expected rows.

This is the "does the instrument layer actually work end-to-end" test —
the same kind of test you'd write before trusting this code with real
instrument output.

HOW TO RUN:
    pytest tests/test_instruments_integration.py -v
"""

import os
import sys
import shutil
import time
import logging
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from instruments.mock_plate_reader import MockPlateReader
from instruments.file_watcher import ResultsWatcher
from instruments.qc_utils import compute_well_qc, plate_summary

logging.basicConfig(level=logging.INFO)


@pytest.fixture
def temp_dirs(tmp_path):
    """Provide isolated incoming/processed directories for each test."""
    incoming = tmp_path / "raw"
    processed = tmp_path / "processed"
    master_csv = processed / "master_results.csv"
    yield incoming, master_csv


class TestMockPlateReader:

    def test_connect_measure_disconnect_cycle(self):
        reader = MockPlateReader(noise_cv=0.03, outlier_prob=0.05)
        reader.connect()
        assert reader.status()["connected"] is True

        result = reader.run_measurement(plate_id="TEST_001")
        assert result.well_data.shape == (8, 12)
        assert result.plate_id == "TEST_001"

        reader.disconnect()
        assert reader.status()["connected"] is False

    def test_measurement_without_connect_raises(self):
        reader = MockPlateReader()
        with pytest.raises(ConnectionError):
            reader.run_measurement(plate_id="SHOULD_FAIL")

    def test_serial_dilution_gradient(self):
        """Column 1 should be higher absorbance than column 12 (dilution gradient)."""
        reader = MockPlateReader(noise_cv=0.01, outlier_prob=0.0)  # low noise for clean check
        reader.connect()
        result = reader.run_measurement(plate_id="GRADIENT_TEST", serial_dilution_start=2.0)
        reader.disconnect()

        col1_mean  = result.well_data[1].mean()
        col12_mean = result.well_data[12].mean()
        assert col1_mean > col12_mean, "Column 1 should have higher absorbance than column 12"


class TestResultsWatcherIntegration:

    def test_single_file_ingestion(self, temp_dirs):
        incoming, master_csv = temp_dirs

        watcher = ResultsWatcher(
            incoming_dir=str(incoming), master_csv=str(master_csv), poll_timeout=0.2
        )
        watcher.start()

        # Generate one plate and write it as a CSV (simulating an instrument)
        reader = MockPlateReader(noise_cv=0.02, outlier_prob=0.0)
        reader.connect()
        result = reader.run_measurement(plate_id="RUN_001")
        reader.disconnect()

        out_path = Path(incoming) / "plate_RUN_001.csv"
        with open(out_path, "w") as f:
            f.write(f"# PLATE_ID=RUN_001\n")
            f.write(result.well_data.to_csv())

        assert watcher.wait_for_count(1, timeout=5.0), \
            f"Watcher did not ingest the file within timeout (processed: {watcher.processed_files})"

        watcher.stop()

        assert master_csv.exists()
        df = pd.read_csv(master_csv)
        assert len(df) == 1
        assert df.iloc[0]["plate_id"] == "RUN_001"
        assert "source_file" in df.columns

    def test_multiple_file_ingestion_in_order(self, temp_dirs):
        incoming, master_csv = temp_dirs

        watcher = ResultsWatcher(
            incoming_dir=str(incoming), master_csv=str(master_csv), poll_timeout=0.2
        )
        watcher.start()

        reader = MockPlateReader(noise_cv=0.02, outlier_prob=0.0)
        reader.connect()

        n_files = 3
        for i in range(1, n_files + 1):
            result = reader.run_measurement(plate_id=f"RUN_{i:03d}")
            out_path = Path(incoming) / f"plate_RUN_{i:03d}.csv"
            with open(out_path, "w") as f:
                f.write(f"# PLATE_ID=RUN_{i:03d}\n")
                f.write(result.well_data.to_csv())

        reader.disconnect()

        assert watcher.wait_for_count(n_files, timeout=10.0), \
            f"Expected {n_files} files ingested, got {watcher.processed_count}"

        watcher.stop()

        df = pd.read_csv(master_csv)
        assert len(df) == n_files
        assert set(df["plate_id"]) == {f"RUN_{i:03d}" for i in range(1, n_files + 1)}

    def test_non_csv_files_ignored(self, temp_dirs):
        incoming, master_csv = temp_dirs

        watcher = ResultsWatcher(
            incoming_dir=str(incoming), master_csv=str(master_csv), poll_timeout=0.2
        )
        watcher.start()

        # Write a non-CSV file — should be ignored
        (Path(incoming) / "notes.txt").write_text("this is not a result file")
        time.sleep(0.8)   # give the watcher a chance to (not) process it

        watcher.stop()

        assert watcher.processed_count == 0
        assert not master_csv.exists()


class TestQCFunctions:

    def test_outlier_detection(self):
        """A column with one extreme outlier should be flagged FAIL."""
        import numpy as np
        data = pd.DataFrame({
            1: [0.5] * 8,             # clean column
            2: [0.5]*7 + [5.0],       # one extreme outlier
        }, index=list("ABCDEFGH"))

        qc = compute_well_qc(data, cv_threshold=15.0)
        assert qc.loc[1, "Status"] == "PASS"
        assert qc.loc[2, "Status"] == "FAIL"

    def test_plate_summary_aggregation(self):
        data = pd.DataFrame({
            i: [1.0/(2**(i-1))]*8 for i in range(1, 13)
        }, index=list("ABCDEFGH"))

        qc = compute_well_qc(data, cv_threshold=15.0)
        summary = plate_summary(qc)

        assert summary["n_columns"] == 12
        assert summary["n_pass"] + summary["n_fail"] == 12
        assert summary["overall_status"] in {"PASS", "FAIL"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
