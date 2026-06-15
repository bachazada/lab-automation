"""
instruments/qc_utils.py

Reusable QC scoring functions for plate-based assay data. Used by the
file watcher, the pipeline summary, and the dashboard — one scoring
implementation, used everywhere.

This module is the equivalent of your drug-target pipeline's
`combine_results` scoring logic — but for plate reader data instead
of docking scores.
"""

import pandas as pd


def compute_well_qc(well_data: pd.DataFrame, cv_threshold: float = 15.0) -> pd.DataFrame:
    """
    QC score each column of a 96-well plate.

    Parameters
    ----------
    well_data : pd.DataFrame
        8 rows (A-H) x 12 columns, values = absorbance (or any readout)
    cv_threshold : float
        CV% above this value flags a column as FAIL (default: 15%)

    Returns
    -------
    pd.DataFrame indexed by Column with: Mean, Std, CV_pct, N_outliers, Status
    """
    means  = well_data.mean()
    stds   = well_data.std()
    cv_pct = (stds / means * 100).round(1)

    # Outlier = any well > mean + 3*std OR < mean - 3*std
    n_outliers = (
        (well_data.sub(means, axis=1).abs() > 3 * stds)
    ).sum()

    qc = pd.DataFrame({
        "Mean":       means.round(4),
        "Std":        stds.round(4),
        "CV_pct":     cv_pct,
        "N_outliers": n_outliers.astype(int),
    })
    qc["Status"] = qc["CV_pct"].apply(lambda x: "PASS" if x < cv_threshold else "FAIL")
    qc.index.name = "Column"
    return qc


def plate_summary(qc_df: pd.DataFrame) -> dict:
    """
    Roll up a per-column QC table into a single plate-level summary.
    """
    n_pass = int((qc_df["Status"] == "PASS").sum())
    n_fail = int((qc_df["Status"] == "FAIL").sum())
    return {
        "n_columns":      len(qc_df),
        "n_pass":         n_pass,
        "n_fail":         n_fail,
        "total_outliers": int(qc_df["N_outliers"].sum()),
        "overall_status": "PASS" if n_fail == 0 else "FAIL",
        "mean_cv_pct":    round(qc_df["CV_pct"].mean(), 1),
    }
