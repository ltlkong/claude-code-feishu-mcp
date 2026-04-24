"""Tests for utils.logging structured helpers."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from xiaobai.utils.logging import (
    bind_request,
    install_jsonl_handler,
    request_id_var,
    span,
)


class BindRequestTests(unittest.TestCase):
    def test_bind_and_restore(self):
        self.assertEqual(request_id_var.get(), "")
        with bind_request("r-123"):
            self.assertEqual(request_id_var.get(), "r-123")
            with bind_request("r-nested"):
                self.assertEqual(request_id_var.get(), "r-nested")
            self.assertEqual(request_id_var.get(), "r-123")
        self.assertEqual(request_id_var.get(), "")


class SpanTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_span_logs_elapsed_ms(self):
        records = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture(level=logging.DEBUG)
        root = logging.getLogger("xiaobai.span")
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            async with span("unit_test", k="v"):
                await asyncio.sleep(0.01)
        finally:
            root.removeHandler(handler)

        ends = [r for r in records if r.getMessage() == "span.end"]
        self.assertEqual(len(ends), 1)
        end = ends[0]
        self.assertEqual(end.span, "unit_test")
        self.assertEqual(end.k, "v")
        self.assertGreaterEqual(end.elapsed_ms, 8)

    async def test_span_records_error_on_exception(self):
        records = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture(level=logging.DEBUG)
        root = logging.getLogger("xiaobai.span")
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with self.assertRaises(ValueError):
                async with span("boom"):
                    raise ValueError("nope")
        finally:
            root.removeHandler(handler)

        ends = [r for r in records if r.getMessage() == "span.end"]
        self.assertEqual(len(ends), 1)
        self.assertTrue(hasattr(ends[0], "error"))
        self.assertIn("ValueError", ends[0].error)


class JsonHandlerTests(unittest.TestCase):
    def test_jsonl_handler_emits_structured_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "out.jsonl"
            # Reset global flag so we can install into a temp file.
            import xiaobai.utils.logging as logging_mod
            logging_mod._jsonl_installed = False
            install_jsonl_handler(str(log_path))
            root = logging.getLogger()
            prev_level = root.level
            root.setLevel(logging.INFO)
            try:
                with bind_request("r-xyz"):
                    logging.getLogger("xiaobai.test").info(
                        "hello %s", "world", extra={"span": "unit", "k": 1}
                    )
                for h in root.handlers:
                    h.flush()
                raw = log_path.read_text().strip().splitlines()
                entries = [json.loads(line) for line in raw if line.strip()]
                last = entries[-1]
                self.assertEqual(last["msg"], "hello world")
                self.assertEqual(last["request_id"], "r-xyz")
                self.assertEqual(last["span"], "unit")
                self.assertEqual(last["k"], 1)
                self.assertEqual(last["level"], "INFO")
                self.assertIn("ts", last)
            finally:
                # Clean up the handler we installed so it doesn't leak
                for h in list(root.handlers):
                    if isinstance(h, logging.FileHandler) and h.baseFilename == str(log_path):
                        root.removeHandler(h)
                        h.close()
                root.setLevel(prev_level)
                logging_mod._jsonl_installed = False


if __name__ == "__main__":
    unittest.main()
