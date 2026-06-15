"""
instruments/mock_plate_reader.py

Simulated 96-well plate reader used throughout the pipeline.

WHY THIS LIVES IN ITS OWN MODULE:
Used by run_pipeline.py, the test suite, and (optionally) the dashboard.
One implementation, one place to fix bugs — the same principle as your
drug-target pipeline's modular rule structure.

USAGE:
    from instruments.mock_plate_reader import MockPlateReader, PlateReadingResult
"""

import time
import random
import logging
from datetime import datetime
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger("plate_reader")


@dataclass
class PlateReadingResult:
    """Structured result from one plate measurement."""
    plate_id:   str
    timestamp:  str
    well_data:  pd.DataFrame
    run_time_s: float

    def to_csv_string(self) -> str:
        lines = [
            f"# PLATE_ID={self.plate_id}",
            f"# TIMESTAMP={self.timestamp}",
            f"# RUN_TIME={self.run_time_s:.2f}s",
            f"# WAVELENGTH=450nm",
            "",
        ]
        lines.append(self.well_data.to_csv())
        return "\n".join(lines)


class MockPlateReader:
    """
    Mock 96-well plate reader. Same public API as a real serial-connected
    instrument would have: connect(), run_measurement(), disconnect().

    Simulates a serial dilution readout with realistic noise + outliers.
    """

    ROWS    = list("ABCDEFGH")
    COLUMNS = list(range(1, 13))

    def __init__(self, noise_cv: float = 0.03, outlier_prob: float = 0.04):
        self.noise_cv     = noise_cv
        self.outlier_prob = outlier_prob
        self.connected    = False
        self._run_count   = 0

    def connect(self):
        time.sleep(0.05)
        self.connected = True
        logger.info("MockPlateReader: connected")

    def disconnect(self):
        self.connected = False
        logger.info("MockPlateReader: disconnected")

    def run_measurement(
        self,
        plate_id: str,
        serial_dilution_start: float = 2.0,
        columns_active: int = 12,
    ) -> PlateReadingResult:
        if not self.connected:
            raise ConnectionError("Call connect() first")

        t_start = time.time()
        self._run_count += 1

        data = {}
        for col in self.COLUMNS:
            col_values = []
            for row in self.ROWS:
                if col <= columns_active:
                    expected   = serial_dilution_start / (2 ** (col - 1))
                    background = random.uniform(0.05, 0.10)
                    std        = expected * self.noise_cv
                    measured   = random.gauss(expected + background, std)
                    measured   = max(0.0001, measured)

                    if random.random() < self.outlier_prob:
                        if random.random() < 0.5:
                            measured = expected * random.uniform(3, 5)
                        else:
                            measured = random.uniform(0.0001, 0.05)
                else:
                    measured = random.gauss(0.075, 0.005)

                col_values.append(round(max(0.0001, measured), 4))
            data[col] = col_values

        well_df = pd.DataFrame(data, index=self.ROWS)
        well_df.index.name   = "Row"
        well_df.columns.name = "Column"

        return PlateReadingResult(
            plate_id=plate_id,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            well_data=well_df,
            run_time_s=time.time() - t_start,
        )

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "runs_completed": self._run_count,
            "model": "MockPlateReader v1.0",
        }
