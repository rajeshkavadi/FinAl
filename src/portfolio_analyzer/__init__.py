"""Portfolio analyzer: MF + direct-equity monitoring and restructuring suggestions."""

from pathlib import Path

try:  # the real installed version, so the UI can display what's running
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("portfolio-analyzer")
except Exception:  # source checkout without install metadata
    __version__ = "0.7.1"

_SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


def sample_path(name: str) -> Path:
    """Absolute path to a bundled sample-data file (packaged as package data)."""
    return _SAMPLE_DIR / name
