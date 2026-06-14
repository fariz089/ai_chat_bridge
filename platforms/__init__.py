"""AI Chat platform capture handlers."""
from .base import LoginCapture, CaptureResult
from .chatgpt import ChatGPTCapture
from .grok import GrokCapture
from .aistudio import AIStudioCapture

PLATFORMS = {
    "chatgpt": ChatGPTCapture,
    "grok": GrokCapture,
    "aistudio": AIStudioCapture,
}

__all__ = ["LoginCapture", "CaptureResult", "PLATFORMS"]
