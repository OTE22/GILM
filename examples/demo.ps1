$ErrorActionPreference = 'Stop'
uv sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --locked python -m gilm demo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run --locked python -m gilm serve
