"""AI Chat platform capture handlers."""
from .base import LoginCapture, CaptureResult
from .chatgpt import ChatGPTCapture
from .grok import GrokCapture
from .aistudio import AIStudioCapture
from .gemini import GeminiCapture

PLATFORMS = {
    "chatgpt": ChatGPTCapture,
    "grok": GrokCapture,
    "aistudio": AIStudioCapture,
    "gemini": GeminiCapture,
}

__all__ = ["LoginCapture", "CaptureResult", "PLATFORMS"]
