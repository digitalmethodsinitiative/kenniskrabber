import subprocess
import os

separator = os.pathsep

subprocess.run([
    "pyinstaller",
    "main.py",
    "--name", "Kenniskrabber",
    "--onedir",
    "--windowed", # Keep this uncommented for the Mac .app bundle!
    "--add-data", f"assets{separator}assets",
    "--add-data", f"README.md{separator}.",
    "--hidden-import", "nicegui",
    "--collect-all", "nicegui",
    "--collect-all", "markdownify",
], check=True)