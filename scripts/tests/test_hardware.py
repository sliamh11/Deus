"""
Tests for evolution/hardware.py — detect_hardware(), recommend_model(), CLI output.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from evolution.hardware import MODEL_SIZES, detect_hardware, recommend_model


# ── MODEL_SIZES ────────────────────────────────────────────────────────────────

class TestModelSizes:
    def test_model_sizes_is_dict(self):
        assert isinstance(MODEL_SIZES, dict)

    def test_all_values_are_positive_floats(self):
        for name, size in MODEL_SIZES.items():
            assert isinstance(size, (int, float)), f"{name} size is not numeric"
            assert size > 0, f"{name} size must be positive"

    def test_known_models_present(self):
        """Key models used by config.py defaults should be present."""
        assert "gemma4:e4b" in MODEL_SIZES
        assert "gemma4:e2b" in MODEL_SIZES

    def test_size_ordering_makes_sense(self):
        """e2b should be smaller than e4b."""
        assert MODEL_SIZES["gemma4:e2b"] < MODEL_SIZES["gemma4:e4b"]


# ── recommend_model() ─────────────────────────────────────────────────────────

class TestRecommendModel:
    def test_returns_tuple_of_str_and_float(self):
        name, size = recommend_model(16.0)
        assert isinstance(name, str)
        assert isinstance(size, float)

    def test_zero_ram_returns_smallest_model(self):
        """With 0 GB RAM nothing fits — smallest model is the fallback."""
        name, size = recommend_model(0.0)
        smallest_size = min(MODEL_SIZES.values())
        assert size == smallest_size

    def test_tiny_ram_returns_smallest_model(self):
        """1 GB RAM can't fit any model — fallback to smallest."""
        name, size = recommend_model(1.0)
        smallest_size = min(MODEL_SIZES.values())
        assert size == smallest_size

    def test_8gb_ram_returns_a_viable_model(self):
        """8 GB RAM should fit the smallest model (3.4 * 1.2 = 4.08 GB)."""
        name, size = recommend_model(8.0)
        # Must fit: size * 1.2 <= 8.0
        assert size * 1.2 <= 8.0, f"Model {name} ({size} GB) doesn't fit in 8 GB RAM"

    def test_16gb_ram_picks_larger_than_8gb(self):
        """16 GB RAM should allow a larger model than 8 GB RAM."""
        name_8, size_8 = recommend_model(8.0)
        name_16, size_16 = recommend_model(16.0)
        assert size_16 >= size_8

    def test_64gb_ram_returns_largest_viable(self):
        """64 GB RAM should fit all models — returns the largest one."""
        name, size = recommend_model(64.0)
        largest_size = max(MODEL_SIZES.values())
        assert size == largest_size

    def test_exact_boundary_fits(self):
        """A model of size S requires S * 1.2 RAM — test exact boundary."""
        # Pick the smallest model
        smallest_name = min(MODEL_SIZES, key=lambda k: MODEL_SIZES[k])
        smallest_size = MODEL_SIZES[smallest_name]
        exact_ram = smallest_size * 1.2
        name, size = recommend_model(exact_ram)
        assert size * 1.2 <= exact_ram

    def test_just_below_boundary_does_not_fit(self):
        """Just below S * 1.2 should not recommend the model if no smaller viable one."""
        smallest_name = min(MODEL_SIZES, key=lambda k: MODEL_SIZES[k])
        smallest_size = MODEL_SIZES[smallest_name]
        just_below_ram = smallest_size * 1.2 - 0.01

        # All models need at least smallest_size * 1.2 RAM
        # None should fit — fallback to smallest
        name, size = recommend_model(just_below_ram)
        assert size == smallest_size  # fallback

    def test_model_name_exists_in_model_sizes(self):
        """Returned model name must be a key in MODEL_SIZES."""
        for ram in [0, 4, 8, 16, 32, 64, 128]:
            name, size = recommend_model(float(ram))
            assert name in MODEL_SIZES, f"recommend_model({ram}) returned unknown model {name}"
            assert MODEL_SIZES[name] == size


# ── detect_hardware() ─────────────────────────────────────────────────────────

class TestDetectHardware:
    def test_returns_dict(self):
        hw = detect_hardware()
        assert isinstance(hw, dict)

    def test_required_keys_present(self):
        hw = detect_hardware()
        for key in ("os", "arch", "ram_gb", "cores", "gpu"):
            assert key in hw, f"Missing key: {key}"

    def test_os_is_string(self):
        hw = detect_hardware()
        assert isinstance(hw["os"], str)
        assert len(hw["os"]) > 0

    def test_ram_gb_is_non_negative(self):
        hw = detect_hardware()
        assert hw["ram_gb"] >= 0, "RAM cannot be negative"

    def test_cores_is_non_negative_int(self):
        hw = detect_hardware()
        assert isinstance(hw["cores"], int)
        assert hw["cores"] >= 0

    def test_gpu_is_string(self):
        hw = detect_hardware()
        assert isinstance(hw["gpu"], str)

    def test_macos_detection(self):
        """On macOS, os should be Darwin and arch arm64 or x86_64."""
        import platform
        if platform.system() != "Darwin":
            pytest.skip("macOS-only test")
        hw = detect_hardware()
        assert hw["os"] == "Darwin"
        assert hw["arch"] in ("arm64", "x86_64")
        assert hw["ram_gb"] > 0
        assert hw["cores"] > 0
        assert hw["gpu"] in ("apple_silicon", "none")

    def test_apple_silicon_gpu_label(self):
        """arm64 macOS should be labeled apple_silicon."""
        import platform
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            pytest.skip("Apple Silicon only")
        hw = detect_hardware()
        assert hw["gpu"] == "apple_silicon"

    @patch("evolution.hardware.platform.system", return_value="Darwin")
    @patch("evolution.hardware.platform.machine", return_value="arm64")
    @patch("evolution.hardware.subprocess.check_output")
    def test_darwin_subprocess_failure_defaults_to_zero(self, mock_out, mock_machine, mock_sys):
        """If sysctl fails on macOS, defaults should be 0."""
        mock_out.side_effect = OSError("sysctl not available")
        hw = detect_hardware()
        assert hw["ram_gb"] == 0.0
        assert hw["cores"] == 0
        assert hw["gpu"] == "apple_silicon"  # arch is still arm64


# ── CLI / JSON output ──────────────────────────────────────────────────────────

class TestCLIOutput:
    def test_cli_prints_valid_json(self):
        """python3 -m evolution.hardware should print parseable JSON."""
        result = subprocess.run(
            [sys.executable, "-m", "evolution.hardware"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert "hardware" in data
        assert "recommendation" in data

    def test_cli_hardware_keys(self):
        result = subprocess.run(
            [sys.executable, "-m", "evolution.hardware"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        data = json.loads(result.stdout)
        hw = data["hardware"]
        for key in ("os", "arch", "ram_gb", "cores", "gpu"):
            assert key in hw, f"Missing hardware key: {key}"

    def test_cli_recommendation_keys(self):
        result = subprocess.run(
            [sys.executable, "-m", "evolution.hardware"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        data = json.loads(result.stdout)
        rec = data["recommendation"]
        assert "model" in rec
        assert "size_gb" in rec
        assert "fits" in rec

    def test_cli_recommendation_model_in_model_sizes(self):
        result = subprocess.run(
            [sys.executable, "-m", "evolution.hardware"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        data = json.loads(result.stdout)
        model = data["recommendation"]["model"]
        assert model in MODEL_SIZES, f"CLI recommended unknown model: {model}"

    def test_cli_fits_matches_ram(self):
        """The 'fits' field should be correct relative to detected RAM."""
        result = subprocess.run(
            [sys.executable, "-m", "evolution.hardware"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        data = json.loads(result.stdout)
        ram = data["hardware"]["ram_gb"]
        size = data["recommendation"]["size_gb"]
        expected_fits = size * 1.2 <= ram
        assert data["recommendation"]["fits"] == expected_fits
