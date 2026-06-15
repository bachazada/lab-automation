"""
tests/test_protocol_generator.py

Unit tests for protocols/protocol_generator.py — specifically the
validate_config() function, which is the safety layer that prevents
bad configs from ever reaching the robot.

HOW TO RUN:
    pytest tests/test_protocol_generator.py -v

WHY THESE TESTS MATTER:
A config validation bug is worse than no validation at all — it gives
false confidence. These tests prove that validate_config() actually
catches the failure modes it claims to catch, AND that it doesn't
reject valid configs (false positives waste lab time).
"""

import copy
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocols.protocol_generator import validate_config, load_config, find_config_path


# ── BASE VALID CONFIG (fixture) ──────────────────────────────────────────────
@pytest.fixture
def base_config():
    """A minimal, valid config — every test starts from a copy of this."""
    return {
        "protocol": {"type": "serial_dilution", "name": "test", "api_level": "2.15"},
        "volumes": {
            "transfer_volume_ul": 100,
            "diluent_volume_ul": 100,
            "max_well_volume_ul": 360,
            "tip_capacity_ul": 300,
        },
        "dilution": {"num_columns": 12, "rows_per_column": 8, "dilution_factor": 2},
        "reagent_dispense": {"target_volume_ul": 50, "target_columns": [1, 2, 3]},
        "media_exchange": {
            "remove_volume_ul": 150, "add_volume_ul": 150,
            "target_columns": [1, 2, 3],
        },
        "biological": {
            "flow_rate_factor": 0.5, "bottom_clearance_mm": 1.5,
            "air_gap_ul": 10, "mix_repetitions": 3, "mix_volume_ul": 75,
            "touch_tip_speed": 20,
        },
        "validation": {"max_cv_threshold": 15.0, "require_positive_volumes": True},
    }


# ── TESTS: VALID CONFIGS PASS ────────────────────────────────────────────────
class TestValidConfigsPass:

    def test_default_serial_dilution_passes(self, base_config):
        """The baseline config should pass with zero errors."""
        validate_config(base_config)  # should not raise

    def test_reagent_dispense_passes(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "reagent_dispense"
        validate_config(config)

    def test_media_exchange_passes(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "media_exchange"
        validate_config(config)


# ── TESTS: INVALID CONFIGS ARE REJECTED ──────────────────────────────────────
class TestInvalidConfigsRejected:

    def test_unknown_protocol_type_rejected(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "not_a_real_protocol"
        with pytest.raises(ValueError, match="protocol.type"):
            validate_config(config)

    def test_tip_overflow_rejected(self, base_config):
        """transfer_volume + air_gap > tip_capacity should fail."""
        config = copy.deepcopy(base_config)
        config["volumes"]["transfer_volume_ul"] = 295
        config["biological"]["air_gap_ul"] = 10   # 295 + 10 = 305 > 300
        with pytest.raises(ValueError, match="tip_capacity_ul"):
            validate_config(config)

    def test_well_overflow_rejected_serial_dilution(self, base_config):
        """diluent + transfer > max_well_volume should fail for serial_dilution."""
        config = copy.deepcopy(base_config)
        config["volumes"]["diluent_volume_ul"] = 300   # 300 + 100 = 400 > 360
        with pytest.raises(ValueError, match="max_well_volume_ul"):
            validate_config(config)

    def test_reagent_dispense_volume_exceeds_well(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "reagent_dispense"
        config["reagent_dispense"]["target_volume_ul"] = 400   # > 360 max well
        with pytest.raises(ValueError, match="max_well_volume_ul"):
            validate_config(config)

    def test_reagent_dispense_invalid_column_rejected(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "reagent_dispense"
        config["reagent_dispense"]["target_columns"] = [1, 2, 13]   # 13 doesn't exist
        with pytest.raises(ValueError, match="target_columns"):
            validate_config(config)

    def test_media_exchange_zero_volume_rejected(self, base_config):
        config = copy.deepcopy(base_config)
        config["protocol"]["type"] = "media_exchange"
        config["media_exchange"]["remove_volume_ul"] = 0
        with pytest.raises(ValueError, match="must be > 0"):
            validate_config(config)

    def test_flow_rate_factor_out_of_range_rejected(self, base_config):
        """flow_rate_factor must be in (0, 1] — test both bounds."""
        config = copy.deepcopy(base_config)

        config["biological"]["flow_rate_factor"] = 0
        with pytest.raises(ValueError, match="flow_rate_factor"):
            validate_config(config)

        config["biological"]["flow_rate_factor"] = 1.5
        with pytest.raises(ValueError, match="flow_rate_factor"):
            validate_config(config)

    def test_negative_volume_rejected(self, base_config):
        config = copy.deepcopy(base_config)
        config["volumes"]["transfer_volume_ul"] = -50
        with pytest.raises(ValueError, match="must be > 0"):
            validate_config(config)

    def test_multiple_errors_all_reported(self, base_config):
        """
        A config with MULTIPLE problems should report ALL of them in one
        exception message — not just the first. This saves round-trips
        when fixing a config.
        """
        config = copy.deepcopy(base_config)
        config["volumes"]["diluent_volume_ul"] = 300       # well overflow
        config["biological"]["flow_rate_factor"] = 2.0     # out of range

        with pytest.raises(ValueError) as exc_info:
            validate_config(config)

        message = str(exc_info.value)
        assert "max_well_volume_ul" in message
        assert "flow_rate_factor" in message


# ── TESTS: CONFIG FILE LOADING ────────────────────────────────────────────────
class TestConfigLoading:

    def test_find_config_path_locates_real_config(self):
        """The actual project config.yaml should be found and load successfully."""
        path = find_config_path()
        assert os.path.exists(path)
        assert path.endswith("config.yaml")

    def test_load_real_config_passes_validation(self):
        """The project's actual config.yaml must itself be valid."""
        config = load_config()   # raises if invalid
        assert config["protocol"]["type"] in {
            "serial_dilution", "reagent_dispense", "media_exchange"
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
