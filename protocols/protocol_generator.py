"""
protocols/protocol_generator.py

Config-driven Opentrons protocol generator. Reads config.yaml and builds
ONE of three assay protocols depending on `protocol.type`:

  - serial_dilution   : dose-response dilution series (Week 1/2 base case)
  - reagent_dispense  : dispense a fixed volume of reagent to selected columns
  - media_exchange    : remove old media + add fresh media (cell culture maintenance)

HOW TO RUN (simulation):
    opentrons_simulate protocols/protocol_generator.py

HOW TO PREVIEW (no robot, just print the plan):
    python3 protocols/protocol_generator.py --preview

HOW TO RUN TESTS:
    pytest tests/test_protocol_generator.py -v
"""

import os
import sys
import yaml

metadata = {
    "apiLevel": "2.15",
    "protocolName": "Lab Automation Suite — Multi-Protocol Generator",
    "author": "Bacha Zada",
}


# ── CONFIG LOADING ────────────────────────────────────────────────────────
def find_config_path() -> str:
    """
    Locate config.yaml relative to the project root.

    NOTE: __file__ is undefined inside Opentrons' exec() context (see Week 2
    Day 12). We fall back through several candidate locations.
    """
    candidates = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "..", "config.yaml"))
        candidates.append(os.path.join(here, "config.yaml"))
    except NameError:
        pass

    cwd = os.getcwd()
    candidates.append(os.path.join(cwd, "config.yaml"))
    candidates.append(os.path.join(cwd, "..", "config.yaml"))

    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)

    raise FileNotFoundError(
        f"config.yaml not found. Checked: {candidates}"
    )


def load_config(config_path: str = None) -> dict:
    if config_path is None:
        config_path = find_config_path()
    with open(config_path) as f:
        config = yaml.safe_load(f)
    validate_config(config)
    return config


# ── VALIDATION ────────────────────────────────────────────────────────────
def validate_config(config: dict) -> None:
    """
    Validate config BEFORE any robot movement. Raises ValueError listing
    ALL problems found (not just the first), so a person fixing the config
    sees everything that needs attention in one pass.
    """
    errors = []
    vol = config["volumes"]
    bio = config["biological"]
    ptype = config["protocol"]["type"]

    valid_types = {"serial_dilution", "reagent_dispense", "media_exchange"}
    if ptype not in valid_types:
        errors.append(f"protocol.type must be one of {valid_types}, got '{ptype}'")

    # Tip capacity check (applies to all protocol types)
    if vol["transfer_volume_ul"] + bio["air_gap_ul"] > vol["tip_capacity_ul"]:
        errors.append(
            f"transfer_volume_ul ({vol['transfer_volume_ul']}) + "
            f"air_gap_ul ({bio['air_gap_ul']}) > tip_capacity_ul ({vol['tip_capacity_ul']})"
        )

    # Well overflow check (serial_dilution specific)
    if ptype == "serial_dilution":
        total = vol["diluent_volume_ul"] + vol["transfer_volume_ul"]
        if total > vol["max_well_volume_ul"]:
            errors.append(
                f"diluent_volume_ul + transfer_volume_ul = {total} > "
                f"max_well_volume_ul ({vol['max_well_volume_ul']})"
            )

    # Reagent dispense checks
    if ptype == "reagent_dispense":
        rd = config.get("reagent_dispense", {})
        if rd.get("target_volume_ul", 0) > vol["max_well_volume_ul"]:
            errors.append(
                f"reagent_dispense.target_volume_ul ({rd.get('target_volume_ul')}) "
                f"> max_well_volume_ul ({vol['max_well_volume_ul']})"
            )
        if rd.get("target_volume_ul", 0) + bio["air_gap_ul"] > vol["tip_capacity_ul"]:
            errors.append(
                f"reagent_dispense.target_volume_ul + air_gap_ul exceeds tip_capacity_ul"
            )
        for col in rd.get("target_columns", []):
            if not (1 <= col <= 12):
                errors.append(f"reagent_dispense.target_columns contains invalid column: {col}")

    # Media exchange checks
    if ptype == "media_exchange":
        me = config.get("media_exchange", {})
        remove_v = me.get("remove_volume_ul", 0)
        add_v    = me.get("add_volume_ul", 0)
        if remove_v <= 0 or add_v <= 0:
            errors.append("media_exchange.remove_volume_ul and add_volume_ul must be > 0")
        if remove_v + bio["air_gap_ul"] > vol["tip_capacity_ul"]:
            errors.append("media_exchange.remove_volume_ul + air_gap_ul exceeds tip_capacity_ul")
        if add_v + bio["air_gap_ul"] > vol["tip_capacity_ul"]:
            errors.append("media_exchange.add_volume_ul + air_gap_ul exceeds tip_capacity_ul")

    # Flow rate factor sanity
    if not (0 < bio["flow_rate_factor"] <= 1):
        errors.append(f"biological.flow_rate_factor must be in (0, 1], got {bio['flow_rate_factor']}")

    # Positive volume check
    if config["validation"]["require_positive_volumes"]:
        for key, value in vol.items():
            if value <= 0:
                errors.append(f"volumes.{key} must be > 0, got {value}")

    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


# ── SHARED HELPERS ────────────────────────────────────────────────────────
def _aspirate_dispense(pipette, volume, source, dest, clearance, air_gap):
    """Aspirate + dispense with the standard biological controls."""
    pipette.aspirate(volume, source.bottom(clearance))
    pipette.air_gap(air_gap)
    pipette.dispense(volume + air_gap, dest.bottom(clearance))


# ── PROTOCOL BUILDERS ─────────────────────────────────────────────────────
def build_serial_dilution(protocol, pipette, plate, reservoir, config):
    """1:N serial dilution across a 96-well plate. (Week 1/2 base case.)"""
    vol = config["volumes"]
    bio = config["biological"]
    dilution = config["dilution"]
    clearance, air_gap = bio["bottom_clearance_mm"], bio["air_gap_ul"]

    protocol.comment(f"[SERIAL_DILUTION] {dilution['num_columns']-1} steps, "
                     f"1:{dilution['dilution_factor']} fold")

    # Step 1: distribute diluent to columns 2-N
    pipette.distribute(
        volume=vol["diluent_volume_ul"],
        source=reservoir["A2"],
        dest=[well for col in plate.columns()[1:dilution["num_columns"]] for well in col],
        new_tip="once",
    )

    # Step 2: load sample into column 1
    pipette.pick_up_tip()
    for well in plate.columns()[0]:
        _aspirate_dispense(pipette, vol["transfer_volume_ul"], reservoir["A1"], well, clearance, air_gap)
    pipette.drop_tip()

    # Step 3: serial dilution
    pipette.pick_up_tip()
    for step in range(dilution["num_columns"] - 1):
        src_col, dst_col = plate.columns()[step], plate.columns()[step + 1]
        for src_well, dst_well in zip(src_col, dst_col):
            _aspirate_dispense(pipette, vol["transfer_volume_ul"], src_well, dst_well, clearance, air_gap)
            pipette.mix(bio["mix_repetitions"], bio["mix_volume_ul"], dst_well.bottom(clearance))
            pipette.blow_out(dst_well.bottom(clearance + 2))
            pipette.touch_tip(speed=bio["touch_tip_speed"])
    pipette.drop_tip()


def build_reagent_dispense(protocol, pipette, plate, reservoir, config):
    """Dispense a fixed volume of reagent to specified columns. Simplest protocol type."""
    vol = config["volumes"]
    bio = config["biological"]
    rd  = config["reagent_dispense"]
    clearance, air_gap = bio["bottom_clearance_mm"], bio["air_gap_ul"]

    target_cols = rd["target_columns"]
    protocol.comment(f"[REAGENT_DISPENSE] {rd['target_volume_ul']} µL to columns {target_cols}")

    dest_wells = [well for col_idx in target_cols for well in plate.columns()[col_idx - 1]]

    pipette.distribute(
        volume=rd["target_volume_ul"],
        source=reservoir["A1"],
        dest=dest_wells,
        new_tip="once",
    )
    protocol.comment(f"[REAGENT_DISPENSE] Complete: {len(dest_wells)} wells dosed")


def build_media_exchange(protocol, pipette, plate, reservoir, config):
    """
    Remove old media, add fresh media. Critical for maintaining cell
    cultures / engineered heart tissues over multi-day experiments.

    Uses TWO reservoir positions: A1 = fresh media source, A12 = waste.
    """
    vol = config["volumes"]
    bio = config["biological"]
    me  = config["media_exchange"]
    clearance, air_gap = bio["bottom_clearance_mm"], bio["air_gap_ul"]

    target_cols = me["target_columns"]
    protocol.comment(f"[MEDIA_EXCHANGE] Remove {me['remove_volume_ul']}µL / "
                     f"Add {me['add_volume_ul']}µL on columns {target_cols}")

    wells = [well for col_idx in target_cols for well in plate.columns()[col_idx - 1]]

    # Phase 1: remove old media -> waste (reservoir A12)
    pipette.pick_up_tip()
    for well in wells:
        # Slightly above the bottom on REMOVAL to avoid disturbing settled cells
        pipette.aspirate(me["remove_volume_ul"], well.bottom(clearance + 0.5))
        pipette.air_gap(air_gap)
        pipette.dispense(me["remove_volume_ul"] + air_gap, reservoir["A12"].top(-2))
        pipette.blow_out(reservoir["A12"].top(-2))
    pipette.drop_tip()

    protocol.comment("[MEDIA_EXCHANGE] Old media removed")

    # Phase 2: add fresh media (reservoir A1), gentle dispense near the wall
    # to avoid disturbing tissue — dispense at well TOP, not bottom
    pipette.pick_up_tip()
    for well in wells:
        pipette.aspirate(me["add_volume_ul"], reservoir["A1"].bottom(clearance))
        pipette.air_gap(air_gap)
        # Dispense near the top of the well, against the wall — gentler than
        # dispensing directly onto the tissue at the bottom
        pipette.dispense(me["add_volume_ul"] + air_gap, well.top(-3))
    pipette.drop_tip()

    protocol.comment("[MEDIA_EXCHANGE] Fresh media added")


# ── DISPATCHER ────────────────────────────────────────────────────────────
PROTOCOL_BUILDERS = {
    "serial_dilution":  build_serial_dilution,
    "reagent_dispense": build_reagent_dispense,
    "media_exchange":   build_media_exchange,
}


def run(protocol):
    """Opentrons entry point. Dispatches to the correct builder based on config."""
    config = load_config()

    protocol.comment("=" * 60)
    protocol.comment(f"[CONFIG] Protocol: {config['protocol']['name']}")
    protocol.comment(f"[CONFIG] Type: {config['protocol']['type']}")
    protocol.comment("=" * 60)

    labware, deck = config["labware"], config["deck"]
    tips      = protocol.load_labware(labware["tip_rack"], deck["tip_rack_slot"])
    reservoir = protocol.load_labware(labware["reservoir"], deck["reservoir_slot"])
    plate     = protocol.load_labware(labware["plate"], deck["plate_slot"])
    pipette   = protocol.load_instrument(labware["pipette"], "right", tip_racks=[tips])

    bio = config["biological"]
    pipette.flow_rate.aspirate *= bio["flow_rate_factor"]
    pipette.flow_rate.dispense *= bio["flow_rate_factor"]

    builder = PROTOCOL_BUILDERS[config["protocol"]["type"]]
    builder(protocol, pipette, plate, reservoir, config)

    protocol.comment("[DONE] Protocol complete")


# ── PREVIEW MODE ──────────────────────────────────────────────────────────
def preview(config_path: str = None):
    """Human-readable description of what the protocol will do."""
    config = load_config(config_path)
    ptype = config["protocol"]["type"]

    print("=" * 60)
    print(f"PROTOCOL PREVIEW: {config['protocol']['name']}")
    print(f"Type: {ptype}")
    print("=" * 60)

    if ptype == "serial_dilution":
        d, v = config["dilution"], config["volumes"]
        print(f"Serial dilution: {d['num_columns']-1} steps, 1:{d['dilution_factor']} fold")
        print(f"Diluent: {v['diluent_volume_ul']}µL/well to columns 2-{d['num_columns']}")
        print(f"Sample: {v['transfer_volume_ul']}µL loaded into column 1")
        print(f"Concentration series:")
        for col in range(1, d["num_columns"] + 1):
            print(f"  Column {col:2d}: 1/{d['dilution_factor']**(col-1)}")

    elif ptype == "reagent_dispense":
        rd = config["reagent_dispense"]
        print(f"Dispense {rd['target_volume_ul']}µL to columns: {rd['target_columns']}")
        n_wells = len(rd["target_columns"]) * 8
        print(f"Total wells: {n_wells}")
        print(f"Total volume: {rd['target_volume_ul'] * n_wells}µL")

    elif ptype == "media_exchange":
        me = config["media_exchange"]
        n_wells = len(me["target_columns"]) * 8
        print(f"Remove {me['remove_volume_ul']}µL / Add {me['add_volume_ul']}µL")
        print(f"Columns: {me['target_columns']} ({n_wells} wells)")
        print(f"Total removed: {me['remove_volume_ul'] * n_wells}µL -> waste (A12)")
        print(f"Total added: {me['add_volume_ul'] * n_wells}µL <- fresh media (A1)")

    bio = config["biological"]
    print(f"\nBiological parameters:")
    print(f"  Flow rate:  {bio['flow_rate_factor']*100:.0f}% of default")
    print(f"  Clearance:  {bio['bottom_clearance_mm']} mm")
    print(f"  Air gap:    {bio['air_gap_ul']} µL")
    print("=" * 60)


if __name__ == "__main__":
    if "--preview" in sys.argv:
        preview()
