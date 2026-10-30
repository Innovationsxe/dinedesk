$ErrorActionPreference = 'Stop'

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Workspace virtual environment not found. Create it with: py -m venv .venv'
}

& $python -m pip install -r (Join-Path $PSScriptRoot 'Backend\requirements.txt')
& $python (Join-Path $PSScriptRoot 'Backend\app.py')
