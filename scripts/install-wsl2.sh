#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# WSL2 Ubuntu; NVIDIA driver belongs on the Windows host.
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg
python3 -m venv .venv-wsl
.venv-wsl/bin/python -m pip install --upgrade pip
.venv-wsl/bin/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv-wsl/bin/python -m pip install -e '.[test]'
.venv-wsl/bin/python -m pip check
.venv-wsl/bin/python -m pytest -q
echo '.venv-wsl/bin/lostmedia serve'
