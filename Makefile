.PHONY: claude gemini cursor gemini-login test compile verify

PYTHON ?= .venv/bin/python

claude:
	claude --dangerously-load-development-channels server:channel --dangerously-skip-permissions --resume

gemini:
	XIAOBAI_PROVIDER=gemini $(PYTHON) -m xiaobai.mcp_server

cursor:
	XIAOBAI_PROVIDER=cursor $(PYTHON) -m xiaobai.mcp_server

gemini-login:
	gemini

test:
	$(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall -q src tests

verify: compile test
