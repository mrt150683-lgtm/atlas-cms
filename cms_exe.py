"""PyInstaller entry point for CMS.exe — see build command in README (Packaging)."""

from cms.cli import app

if __name__ == "__main__":
    app()
