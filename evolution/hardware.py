"""
Hardware detection and Ollama model recommendation for Deus.

Provides a reusable detect_hardware() function and MODEL_SIZES registry
used by the setup model advisor and the benchmark_judge module.

CLI interface:
    python3 -m evolution.hardware
    Prints JSON with hardware info and recommended model.
"""
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Model size estimates (download size in GB).
# Ordered from smallest to largest so recommend_model() can pick the largest viable.
MODEL_SIZES: dict[str, float] = {
    "qwen3.5:4b": 3.4,
    "gemma4:e2b": 7.2,
    "gemma4:e4b": 9.6,
    "gemma4:26b": 18.0,
    "gemma4:31b": 20.0,
}


def detect_hardware() -> dict:
    """Detect system hardware for model recommendations.

    Returns a dict with keys: os, arch, ram_gb, cores, gpu.
    Values are best-effort — missing fields default to 0 or 'unknown'.
    Works on macOS, Linux, and Windows.
    """
    hw: dict = {
        "os": platform.system(),
        "arch": platform.machine(),
        "ram_gb": 0.0,
        "cores": 0,
        "gpu": "unknown",
    }

    if hw["os"] == "Darwin":
        # macOS — use sysctl
        try:
            ram = int(
                subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"],
                    stderr=subprocess.DEVNULL,
                ).strip()
            )
            hw["ram_gb"] = ram / (1024 ** 3)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass

        try:
            cores = int(
                subprocess.check_output(
                    ["sysctl", "-n", "hw.ncpu"],
                    stderr=subprocess.DEVNULL,
                ).strip()
            )
            hw["cores"] = cores
        except (subprocess.SubprocessError, ValueError, OSError):
            pass

        # Apple Silicon has unified memory — GPU shares the same pool
        hw["gpu"] = "apple_silicon" if hw["arch"] == "arm64" else "none"

    elif hw["os"] == "Linux":
        import os as _os

        hw["cores"] = _os.cpu_count() or 0

        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        hw["ram_gb"] = int(line.split()[1]) / (1024 ** 2)
                        break
        except (OSError, ValueError):
            pass

        # Check for NVIDIA GPU
        try:
            subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL,
            )
            hw["gpu"] = "nvidia"
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            hw["gpu"] = "none"

    elif hw["os"] == "Windows":
        import os as _os

        hw["cores"] = _os.cpu_count() or 0

        # Use wmic to get total physical memory in bytes
        try:
            out = subprocess.check_output(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
            )
            for line in out.splitlines():
                if line.startswith("TotalPhysicalMemory="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        hw["ram_gb"] = int(val) / (1024 ** 3)
                        break
        except (subprocess.SubprocessError, ValueError, OSError):
            pass

        # Check for NVIDIA GPU on Windows
        try:
            subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL,
            )
            hw["gpu"] = "nvidia"
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            hw["gpu"] = "unknown"

    return hw


def recommend_model(ram_gb: float) -> tuple[str, float]:
    """Return (model_name, model_size_gb) for the largest model that fits in ram_gb.

    A model is considered viable when its size * 1.2 <= ram_gb (20% headroom).
    If no model fits, returns the smallest model as a fallback with a caveat
    (caller should check whether ram_gb is sufficient).

    Returns the smallest model as fallback if nothing fits.
    """
    viable = {
        name: size
        for name, size in MODEL_SIZES.items()
        if size * 1.2 <= ram_gb
    }

    if viable:
        best = max(viable, key=lambda k: viable[k])
        return best, viable[best]

    # Nothing fits — return smallest model as fallback
    smallest = min(MODEL_SIZES, key=lambda k: MODEL_SIZES[k])
    return smallest, MODEL_SIZES[smallest]


def _main() -> None:
    """CLI entry point: print JSON hardware report to stdout."""
    hw = detect_hardware()
    model_name, model_size = recommend_model(hw.get("ram_gb", 0.0))

    output = {
        "hardware": hw,
        "recommendation": {
            "model": model_name,
            "size_gb": model_size,
            "fits": model_size * 1.2 <= hw.get("ram_gb", 0.0),
        },
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    _main()
