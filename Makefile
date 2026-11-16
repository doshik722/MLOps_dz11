.PHONY: sync build up status check local-check a balance c report down
sync:
	uv sync --locked
build:
	pwsh -NoProfile -File stand.ps1 build
up:
	pwsh -NoProfile -File stand.ps1 up
status:
	pwsh -NoProfile -File stand.ps1 status
local-check:
	uv run python local_check.py
check:
	uv run python -m unittest discover -s tests -v
a:
	uv run python -u experiments.py a
balance:
	uv run python -u experiments.py balance
c:
	uv run python -u experiments.py c
report:
	uv run python report.py
down:
	pwsh -NoProfile -File stand.ps1 down
