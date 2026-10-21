$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Install Python 3.11+ from python.org first.' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    winget install --id Gyan.FFmpeg --exact
    throw 'FFmpeg installed. Open a new terminal to refresh PATH and rerun this script.'
}
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$projectPython = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $projectPython -m pip install --upgrade pip
& $projectPython -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed.' }
& $projectPython -m pip install -e '.[test]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $projectPython -m pip check
& $projectPython -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
Write-Host 'Ready: .\.venv\Scripts\lostmedia serve'
