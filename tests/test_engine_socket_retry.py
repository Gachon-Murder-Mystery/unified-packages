"""FIX-I17: a connection that could not be opened (WinError 10055) is sent again; engine answers are not."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from GM_Agent_Society import engine_v1   # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def socket_error():
    err = OSError("no buffer space"); err.winerror = 10055
    return urllib.error.URLError(err)


class RetryTests(unittest.TestCase):
    def test_no_buffer_space_is_retried(self):
        calls = []
        def urlopen(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise socket_error()
            return Resp(b'{"ok": true}')
        with mock.patch.object(engine_v1.urllib.request, "urlopen", urlopen), mock.patch.object(engine_v1.time, "sleep", lambda s: None):
            self.assertEqual(engine_v1.EngineV1Client().call("x"), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_an_engine_error_is_sent_once(self):
        calls = []
        def urlopen(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError("u", 409, "conflict", {}, io.BytesIO(b'{"code": "conflict"}'))
        with mock.patch.object(engine_v1.urllib.request, "urlopen", urlopen):
            with self.assertRaises(engine_v1.EngineV1Error):
                engine_v1.EngineV1Client().call("x")
        self.assertEqual(len(calls), 1)

    def test_other_connection_errors_are_raised(self):
        def urlopen(req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(engine_v1.urllib.request, "urlopen", urlopen):
            with self.assertRaises(urllib.error.URLError):
                engine_v1.EngineV1Client().call("x")


if __name__ == "__main__":
    unittest.main()
