import json
import sys
import tempfile
import unittest
from pathlib import Path

from xiaobai.core.hooks import HookRunner
from xiaobai.mcp_server import XiaobaiServer


class HookRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_tool_use_hook_receives_tool_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "hook_output.jsonl"
            settings = tmp_path / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f"{sys.executable} -c \"import json,sys; "
                                                f"open('{output_path}','a').write(sys.stdin.read())\""
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            runner = HookRunner(settings)
            await runner.run_post_tool_use(
                tool_name="reply",
                tool_input={"chat_id": "c1", "text": "hi"},
                tool_response={"status": "ok"},
            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tool_name"], "reply")
            self.assertEqual(payload["tool_input"]["chat_id"], "c1")
            self.assertEqual(payload["tool_response"]["status"], "ok")


class ProviderHookDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_dispatch_runs_post_tool_use_hook(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server._short_ids = type("ShortIds", (), {
            "resolve_message": lambda self, value: value,
            "resolve_request": lambda self, value: value,
        })()
        seen = {}

        class FakeHookRunner:
            async def run_post_tool_use(self, *, tool_name, tool_input, tool_response):
                seen["tool_name"] = tool_name
                seen["tool_input"] = tool_input
                seen["tool_response"] = tool_response

        async def fake_dispatch(name, arguments):
            return {"status": "ok", "name": name, "args": arguments}

        server._hook_runner = FakeHookRunner()
        server._dispatch_tool = fake_dispatch

        result = await server._dispatch_provider_tool(
            "reply", {"chat_id": "c1", "text": "hi"}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen["tool_name"], "reply")
        self.assertEqual(seen["tool_input"]["chat_id"], "c1")
        self.assertEqual(seen["tool_response"]["status"], "ok")
