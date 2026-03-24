import subprocess
import os

subprocess.run([
    "pyinstaller",
    "main.py",
    "--name", "Kenniskrabber",
    "--onedir",
    "--windowed",
    "--add-data", "assets;assets",
    "--add-data", "README.md;.",
    "--hidden-import", "nicegui",
    "--collect-all", "nicegui",
    "--collect-all", "markdownify",
], check=True)