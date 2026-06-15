"""
dashboard/app.py

Lab Automation Suite — QC Dashboard

Reads data/processed/master_results.csv (written by ResultsWatcher) and
the most recent raw plate files in data/raw/ to show:

  - Run history with quality ranking and drift detection
  - Heatmap of the most recent plate
  - Run comparison (any two runs side-by-side)
  - Config viewer (shows the active config.yaml)
  - CSV upload for manual ingestion

HOW TO RUN:
    streamlit run dashboard/app.py

This is the "front end" of the suite — what a wet-lab scientist at
myotwin would actually look at after a robot run completes.
"""

import os
import sys
import glob
from pathlib import Path

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from instruments.qc_utils import compute_well_qc, plate_summary
from protocols.protocol_generator import load_config, find_config_path


st.set_page_config(page_title="Lab Automation Suite — myotwin Prep", layout="wide")

st.title("Lab Automation Suite")
st.caption("Mini lab automation pipeline — Bacha Zada — built in preparation for myotwin GmbH")


# ── PATHS ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR       = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
MASTER_CSV    = PROCESSED_DIR / "master_results.csv"


# ── SIDEBAR: CONFIG VIEWER ────────────────────────────────────────────────
st.sidebar.header("Active Configuration")
try:
    config = load_config()
    st.sidebar.markdown(f"**Protocol:** {config['protocol']['name']}")
    st.sidebar.markdown(f"**Type:** `{config['protocol']['type']}`")
    cv_threshold = config["validation"]["max_cv_threshold"]
    st.sidebar.markdown(f"**CV% threshold:** {cv_threshold}%")
    with st.sidebar.expander("Full config.yaml"):
        st.code(yaml.dump(config), language="yaml")
except Exception as e:
    st.sidebar.error(f"Could not load config: {e}")
    cv_threshold = 15.0


# ── SIDEBAR: CSV UPLOAD ────────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.header("Manual Upload")
uploaded = st.sidebar.file_uploader("Upload a plate result CSV", type="csv")
if uploaded is not None:
    try:
        df_uploaded = pd.read_csv(uploaded, comment="#", index_col=0)
        df_uploaded.columns = [int(c) for c in df_uploaded.columns]
        qc = compute_well_qc(df_uploaded, cv_threshold=cv_threshold)
        summary = plate_summary(qc)

        st.sidebar.success(
            f"Parsed: {summary['n_pass']}/{summary['n_columns']} columns PASS, "
            f"overall={summary['overall_status']}"
        )

        st.session_state["uploaded_plate"] = df_uploaded
        st.session_state["uploaded_qc"] = qc
    except Exception as e:
        st.sidebar.error(f"Failed to parse CSV: {e}")


# ── MAIN: RUN HISTORY ────────────────────────────────────────────────────
st.subheader("Run History")

if MASTER_CSV.exists():
    history_df = pd.read_csv(MASTER_CSV)

    # Add derived columns (same logic as Week 2 Day 10 consolidator)
    history_df["pass_rate"] = (history_df["n_pass"] / (history_df["n_pass"] + history_df["n_fail"])).round(3)
    history_df["rolling_mean_cv"] = history_df["mean_cv_pct"].rolling(window=3, min_periods=1).mean().round(1)

    # quality_rank: primary sort = pass_rate (desc), tiebreaker = mean_cv_pct (asc)
    rank_order = history_df.sort_values(["pass_rate", "mean_cv_pct"], ascending=[False, True])
    rank_map = {pid: rank for rank, pid in enumerate(rank_order["plate_id"], start=1)}
    history_df["quality_rank"] = history_df["plate_id"].map(rank_map)

    # Color-code overall_status
    def highlight_status(row):
        color = "#d4edda" if row["overall_status"] == "PASS" else "#f8d7da"
        return [f"background-color: {color}"] * len(row)

    st.dataframe(history_df.style.apply(highlight_status, axis=1), use_container_width=True)

    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Runs", len(history_df))
    col2.metric("Runs PASS", int((history_df["overall_status"] == "PASS").sum()))
    col3.metric("Mean CV% (all runs)", f"{history_df['mean_cv_pct'].mean():.1f}%")
    latest_cv = history_df["mean_cv_pct"].iloc[-1]
    earliest_cv = history_df["mean_cv_pct"].iloc[0]
    trend = "↑ increasing" if latest_cv > earliest_cv else "↓ decreasing" if latest_cv < earliest_cv else "→ stable"
    col4.metric("CV% Trend", trend)

    # CV% trend chart
    if len(history_df) >= 2:
        st.subheader("CV% Trend Across Runs")
        chart_df = history_df.set_index("plate_id")[["mean_cv_pct", "rolling_mean_cv"]]
        st.line_chart(chart_df)

else:
    st.info(
        "No run history found yet. Run `python3 run_pipeline.py` first to "
        "generate plates and populate the results database."
    )
    history_df = pd.DataFrame()


st.divider()


# ── MAIN: LATEST PLATE HEATMAP ────────────────────────────────────────────
st.subheader("Latest Plate Heatmap")

raw_files = sorted(glob.glob(str(RAW_DIR / "plate_*.csv")))

if raw_files:
    latest_file = raw_files[-1]
    plate_df = pd.read_csv(latest_file, comment="#", index_col=0)
    plate_df.columns = [int(c) for c in plate_df.columns]

    qc = compute_well_qc(plate_df, cv_threshold=cv_threshold)
    summary = plate_summary(qc)

    st.markdown(f"**File:** `{os.path.basename(latest_file)}` — "
                f"Overall: **{summary['overall_status']}** | "
                f"Columns PASS: {summary['n_pass']}/{summary['n_columns']} | "
                f"Mean CV%: {summary['mean_cv_pct']}%")

    fig, ax = plt.subplots(figsize=(11, 4))
    sns.heatmap(plate_df, annot=True, fmt=".3f", cmap="viridis",
                linewidths=0.5, linecolor="white",
                cbar_kws={"label": "Absorbance (OD)"}, ax=ax)
    ax.set_xlabel("Column (1 = undiluted)")
    ax.set_ylabel("Row")
    st.pyplot(fig)
    plt.close(fig)

    with st.expander("QC table for this plate"):
        st.dataframe(qc, use_container_width=True)

elif "uploaded_plate" in st.session_state:
    plate_df = st.session_state["uploaded_plate"]
    qc = st.session_state["uploaded_qc"]
    summary = plate_summary(qc)

    st.markdown(f"**Uploaded plate** — Overall: **{summary['overall_status']}** | "
                f"Columns PASS: {summary['n_pass']}/{summary['n_columns']}")

    fig, ax = plt.subplots(figsize=(11, 4))
    sns.heatmap(plate_df, annot=True, fmt=".3f", cmap="viridis",
                linewidths=0.5, linecolor="white", ax=ax)
    st.pyplot(fig)
    plt.close(fig)

else:
    st.info("No plate data found. Run the pipeline or upload a CSV in the sidebar.")


st.divider()


# ── MAIN: RUN COMPARISON ───────────────────────────────────────────────────
if len(raw_files) >= 2:
    st.subheader("Run Comparison")

    file_options = {os.path.basename(f): f for f in raw_files}
    names = list(file_options.keys())

    c1, c2 = st.columns(2)
    with c1:
        sel_a = st.selectbox("Run A", names, index=max(0, len(names) - 2))
    with c2:
        sel_b = st.selectbox("Run B", names, index=len(names) - 1)

    def load_plate(fname):
        df = pd.read_csv(file_options[fname], comment="#", index_col=0)
        df.columns = [int(c) for c in df.columns]
        return df

    plate_a, plate_b = load_plate(sel_a), load_plate(sel_b)
    qc_a = compute_well_qc(plate_a, cv_threshold=cv_threshold)
    qc_b = compute_well_qc(plate_b, cv_threshold=cv_threshold)

    cc1, cc2 = st.columns(2)
    for col, (name, plate, qc) in zip([cc1, cc2], [(sel_a, plate_a, qc_a), (sel_b, plate_b, qc_b)]):
        with col:
            summary = plate_summary(qc)
            st.markdown(f"**{name}** — {summary['overall_status']} "
                        f"(mean CV%: {summary['mean_cv_pct']}%)")
            fig, ax = plt.subplots(figsize=(5.5, 4))
            sns.heatmap(plate, annot=False, cmap="viridis", cbar=False, ax=ax)
            st.pyplot(fig)
            plt.close(fig)

    # Status changes between runs
    changed = []
    for col_name in qc_a.index:
        sa, sb = qc_a.loc[col_name, "Status"], qc_b.loc[col_name, "Status"]
        if sa != sb:
            changed.append((col_name, sa, sb))

    if changed:
        st.warning("Columns that changed PASS/FAIL status between these runs:")
        for col_name, sa, sb in changed:
            st.write(f"  Column {col_name}: {sa} → {sb}")
    else:
        st.success("No columns changed PASS/FAIL status between these two runs.")


st.divider()


# ── EXPORT ────────────────────────────────────────────────────────────────
st.subheader("Export")
if not history_df.empty:
    csv_export = history_df.to_csv(index=False)
    st.download_button(
        "Download Run History (CSV)",
        data=csv_export,
        file_name="run_history_export.csv",
        mime="text/csv",
    )
else:
    st.caption("Nothing to export yet — run the pipeline first.")
