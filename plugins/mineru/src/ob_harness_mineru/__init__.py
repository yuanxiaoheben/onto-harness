"""Installation registers the parser; importing it does not load MinerU or models."""

from .plugin import MinerUPlugin

plugin = MinerUPlugin()
