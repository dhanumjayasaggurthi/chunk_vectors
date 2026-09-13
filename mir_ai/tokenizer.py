from __future__ import annotations


class TokenCounter:
    def __init__(self, model: str = "text-embedding-3-large"):
        try:
            import tiktoken
            try:
                self._enc = tiktoken.encoding_for_model(model)
            except KeyError:
                self._enc = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            self._enc = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, (len(text) + 2) // 3)
