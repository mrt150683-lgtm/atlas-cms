"""Native folder-picker dialog.

Atlas runs through python (CMS.bat), so tkinter is available for a real OS
"choose folder" dialog at launch. Returns None when there's no display or
tkinter (headless / server), so callers can fall back to a console prompt.
"""

from __future__ import annotations


def pick_folder(title: str = "Choose the codebase folder for Atlas") -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        path = filedialog.askdirectory(title=title)
        root.update()
        root.destroy()
        return path or None
    except Exception:
        return None
