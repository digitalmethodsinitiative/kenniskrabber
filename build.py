import subprocess
import os

subprocess.run([
    "pyinstaller",
    "main.py",
    "--name", "Kenniskrabber",
    "--onedir",
    "--windowed",
    "--icon", "logo_cutout.png",
    "--add-data", "README.md;.",
    "--add-data", "stylesheet.css;.",
    "--add-data", "logo_cutout.png;.",
    "--hidden-import", "nicegui",
    "--collect-all", "nicegui",
    "--collect-all", "markdownify",
], check=True)