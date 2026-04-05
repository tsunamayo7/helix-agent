"""Smoke tests for src.ja_input — the Japanese input helper CLI.

We avoid launching the actual tkinter window (needs a display, blocks on
mainloop) and instead verify that the module imports cleanly, exposes the
expected entry points, and that the window-construction helper returns the
expected widget types when a display is available.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_module_imports():
    """ja_input must import without touching tk display until launch()."""
    mod = importlib.import_module("src.ja_input")
    assert hasattr(mod, "main")
    assert hasattr(mod, "launch")
    assert callable(mod.main)
    assert callable(mod.launch)


def test_constants_defined():
    """Window title and labels must be set and non-empty."""
    from src import ja_input

    assert ja_input.WINDOW_TITLE
    assert ja_input.LABEL_TEXT
    assert ja_input.HINT_TEXT
    # Verify the Japanese content actually contains Japanese characters
    assert "日本語" in ja_input.LABEL_TEXT
    assert "Ctrl+Enter" in ja_input.LABEL_TEXT


def test_font_selection_by_platform():
    """Windows gets Meiryo UI, other platforms get TkDefaultFont."""
    from src import ja_input

    font_name, font_size = ja_input.DEFAULT_FONT
    assert isinstance(font_size, int)
    if sys.platform.startswith("win"):
        assert font_name == "Meiryo UI"
    else:
        assert font_name == "TkDefaultFont"


@pytest.mark.skipif(
    sys.platform == "linux" and not __import__("os").environ.get("DISPLAY"),
    reason="tkinter needs a display on Linux",
)
def test_window_construction_smoke():
    """Verify _create_window returns (root, text_widget, status_label) tuple.

    Immediately destroys the window to avoid blocking. This runs on Windows
    and macOS reliably; on headless Linux CI we skip it.
    """
    import tkinter as tk

    from src import ja_input

    try:
        root, text, status = ja_input._create_window()
    except tk.TclError:
        pytest.skip("tkinter cannot create display")
        return

    try:
        assert isinstance(root, tk.Tk)
        assert root.title() == ja_input.WINDOW_TITLE
        # text widget must accept insert/get
        text.insert("1.0", "テスト")
        assert text.get("1.0", "end-1c") == "テスト"
        # status label is a Label with a text option
        assert status.cget("text") == ja_input.HINT_TEXT
    finally:
        root.destroy()
