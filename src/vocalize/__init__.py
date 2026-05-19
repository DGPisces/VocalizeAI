"""VocalizeAI — bilingual restaurant-reservation telephony agent."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vocalize-ai")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
