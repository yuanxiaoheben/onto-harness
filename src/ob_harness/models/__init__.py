"""Shared API/local HTTP and explicitly labelled replay inference adapters."""

from .inference import ChatModel, ReplayModel, build_model

__all__ = ["ChatModel", "ReplayModel", "build_model"]
