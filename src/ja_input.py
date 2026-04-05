"""helix-agent ja-input: Floating Japanese input helper for Claude Code on Windows.

Solves the React Ink + IME incompatibility that breaks Japanese input in
Claude Code's terminal (character duplication, cursor drift, conversion
candidates not displaying). Opens a small always-on-top window that uses
the OS-native IME, then copies the text to the clipboard so the user can
paste it cleanly into the Claude Code terminal with Ctrl+V.

Design goals:
- stdlib-only (tkinter) — zero extra dependencies
- Cross-platform (Windows primary, also runs on macOS/Linux)
- No window-focus manipulation (keeps behavior predictable)

Usage:
    uv run helix-agent-ja-input
    # or
    uv run python -m src.ja_input
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import scrolledtext


WINDOW_TITLE = "helix-agent — 日本語入力ヘルパー"
LABEL_TEXT = "日本語で入力 → Ctrl+Enter でコピー → ターミナルで Ctrl+V"
HINT_TEXT = "Esc: 閉じる  |  Ctrl+L: クリア"
DEFAULT_FONT = ("Meiryo UI", 11) if sys.platform.startswith("win") else ("TkDefaultFont", 11)


def _create_window() -> tuple[tk.Tk, scrolledtext.ScrolledText, tk.Label]:
    """Build the input window and return (root, text_widget, status_label)."""
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry("640x320")
    root.attributes("-topmost", True)

    # Header label
    header = tk.Label(root, text=LABEL_TEXT, anchor="w", font=DEFAULT_FONT)
    header.pack(fill="x", padx=10, pady=(10, 0))

    # Multi-line text input
    text = scrolledtext.ScrolledText(
        root, wrap="word", font=DEFAULT_FONT, undo=True
    )
    text.pack(fill="both", expand=True, padx=10, pady=8)
    text.focus_set()

    # Status line at bottom
    status = tk.Label(root, text=HINT_TEXT, anchor="w", fg="#666666", font=DEFAULT_FONT)
    status.pack(fill="x", padx=10, pady=(0, 4))

    return root, text, status


def _bind_shortcuts(
    root: tk.Tk,
    text: scrolledtext.ScrolledText,
    status: tk.Label,
) -> None:
    """Wire keyboard shortcuts and button actions to the window."""

    def copy_to_clipboard(event=None):
        content = text.get("1.0", "end-1c")
        if not content.strip():
            status.config(text="(入力が空です)", fg="#cc0000")
            return "break"
        root.clipboard_clear()
        root.clipboard_append(content)
        root.update()  # ensure clipboard is committed before we clear the widget
        char_count = len(content)
        status.config(
            text=f"✓ コピー完了（{char_count} 文字）— ターミナルで Ctrl+V",
            fg="#0a7c2f",
        )
        text.delete("1.0", "end")
        return "break"

    def clear_text(event=None):
        text.delete("1.0", "end")
        status.config(text=HINT_TEXT, fg="#666666")
        return "break"

    def close_app(event=None):
        root.destroy()
        return "break"

    text.bind("<Control-Return>", copy_to_clipboard)
    text.bind("<Control-l>", clear_text)
    text.bind("<Control-L>", clear_text)
    root.bind("<Escape>", close_app)

    # Button row
    button_frame = tk.Frame(root)
    button_frame.pack(fill="x", padx=10, pady=(0, 10))
    copy_btn = tk.Button(
        button_frame, text="コピー (Ctrl+Enter)", command=copy_to_clipboard
    )
    copy_btn.pack(side="left")
    clear_btn = tk.Button(button_frame, text="クリア (Ctrl+L)", command=clear_text)
    clear_btn.pack(side="left", padx=(8, 0))
    close_btn = tk.Button(button_frame, text="閉じる (Esc)", command=close_app)
    close_btn.pack(side="right")


def launch() -> None:
    """Open the Japanese input helper window."""
    root, text, status = _create_window()
    _bind_shortcuts(root, text, status)
    root.mainloop()


def main() -> None:
    """Entry point for the `helix-agent-ja-input` console script."""
    launch()


if __name__ == "__main__":
    main()
