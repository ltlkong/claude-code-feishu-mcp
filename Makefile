.PHONY: claude gemini gemini-login test compile verify

PYTHON ?= .venv/bin/python

claude:
	claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome

gemini:
	XIAOBAI_PROVIDER=gemini $(PYTHON) -m xiaobai.mcp_server

gemini-login:
	gemini

test:
	$(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall -q src tests

verify: compile test
