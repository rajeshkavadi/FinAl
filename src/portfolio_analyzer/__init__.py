"""Portfolio analyzer: MF + direct-equity monitoring and restructuring suggestions."""

from pathlib import Path

__version__ = "0.1.0"

_SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


def sample_path(name: str) -> Path:
    """Absolute path to a bundled sample-data file (packaged as package data)."""
    return _SAMPLE_DIR / name
