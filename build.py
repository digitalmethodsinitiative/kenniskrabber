import subprocess
import os
import platform

separator = os.pathsep

if platform.system() == "Darwin":
    icon_file = "assets/icon.icns"
else:
    icon_file = "assets/icon.ico"

subprocess.run([
    "pyinstaller",
    "main.py",
    "--name", "Kenniskrabber",
    "--onedir",
    "--windowed",
    "--icon", icon_file,
    "--add-data", f"assets{separator}assets",
    "--add-data", f"README.md{separator}.",
    "--add-data", f"css-selectors.json{separator}.",
    "--hidden-import", "nicegui",
    "--collect-all", "nicegui",
    "--collect-all", "markdownify",
    "--collect-all", "selenium"
], check=True)