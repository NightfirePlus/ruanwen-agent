import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ima import ImaClient, ImaError, mask_secret  # noqa: E402

FAKE_ID = "test-client-id-0000000000000000"
FAKE_KEY = "test-api-key-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self, *_):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def client():
    return ImaClient(client_id=FAKE_ID, api_key=FAKE_KEY, max_retries=1)


class MaskTest(unittest.TestCase):
    def test_mask(self):
        m = mask_secret(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, m)
        self.assertTrue(m.startswith("tes***"))

    def test_short_value_fully_masked(self):
        self.assertEqual(mask_secret("abcd"), "****")


class ClientTest(unittest.TestCase):
    def test_missing_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImaError):
                ImaClient()

    def test_search_knowledge_base_accepts_both_field_names(self):
        payload = {
            "code": 0,
            "msg": "ok",
            "data": {"info_list": [{"kb_id": "K1", "kb_name": "A"}, {"id": "K2", "name": "B"}]},
        }
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)):
            r = client().search_knowledge_base("x")
        self.assertEqual(r, [{"kb_id": "K1", "kb_name": "A"}, {"kb_id": "K2", "kb_name": "B"}])

    def test_request_carries_headers_and_body(self):
        payload = {"code": 0, "msg": "ok", "data": {"knowledge_list": [], "is_end": True}}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)) as m:
            client().get_knowledge_list("KB", folder_id="F1")
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/openapi/wiki/v1/get_knowledge_list"))
        self.assertEqual(req.get_header("Ima-openapi-clientid"), FAKE_ID)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["folder_id"], "F1")
        self.assertEqual(body["knowledge_base_id"], "KB")

    def test_non_retryable_error_raises_without_secret(self):
        payload = {"code": 110030, "msg": "无权限", "data": {}}
        c = client()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)) as m:
            with self.assertRaises(ImaError) as ctx:
                c.search_knowledge("KB", "x")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(ctx.exception.code, 110030)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_retryable_error_retries(self):
        bad = FakeResp({"code": 110010, "msg": "net", "data": {}})
        good = FakeResp({"code": 0, "msg": "ok", "data": {"info_list": [], "is_end": True}})
        with mock.patch("urllib.request.urlopen", side_effect=[bad, good]), mock.patch("time.sleep"):
            r = client().search_knowledge("KB", "x")
        self.assertEqual(r["items"], [])

    def test_call_log_has_no_credentials(self):
        payload = {"code": 0, "msg": "ok", "data": {}}
        c = client()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)):
            c.get_media_info("M1")
        dumped = json.dumps([r.__dict__ for r in c.call_log])
        self.assertNotIn(FAKE_KEY, dumped)
        self.assertNotIn(FAKE_ID, dumped)

    def test_credential_status_is_masked(self):
        s = client().credential_status()
        self.assertNotEqual(s["api_key"], FAKE_KEY)
        self.assertIn("***", s["api_key"])

    def test_empty_media_id_rejected(self):
        with self.assertRaises(ImaError):
            client().get_media_info("")


if __name__ == "__main__":
    unittest.main()
