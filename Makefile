.PHONY: claude gemini test compile verify

PYTHON ?= .venv/bin/python

claude:
	claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome

gemini:
	XIAOBAI_PROVIDER=gemini $(PYTHON) -m xiaobai.mcp_server

test:
	$(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall -q src tests

verify: compile test
