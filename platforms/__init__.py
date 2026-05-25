"""AI Chat platform capture handlers."""
from .base import LoginCapture, CaptureResult
from .chatgpt import ChatGPTCapture
from .grok import GrokCapture

PLATFORMS = {
    "chatgpt": ChatGPTCapture,
    "grok": GrokCapture,
}

__all__ = ["LoginCapture", "CaptureResult", "PLATFORMS"]
