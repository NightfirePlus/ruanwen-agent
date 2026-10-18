import json
import os
import unittest
from unittest import mock

from helpers import FakeLLM  # noqa: F401  (确保 sys.path 已设置)

from agent.ima import ImaClient, ImaError, mask_secret  # noqa: E402

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


class ImaTest(unittest.TestCase):
    def test_mask(self):
        self.assertNotIn(FAKE_KEY, mask_secret(FAKE_KEY))
        self.assertEqual(mask_secret("abcd"), "****")

    def test_missing_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImaError):
                ImaClient()

    def test_both_kb_field_names(self):
        payload = {"code": 0, "msg": "ok", "data": {"info_list": [{"kb_id": "K1", "kb_name": "A"}, {"id": "K2", "name": "B"}]}}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)):
            self.assertEqual(client().search_knowledge_base("x"), [{"kb_id": "K1", "kb_name": "A"}, {"kb_id": "K2", "kb_name": "B"}])

    def test_request_shape(self):
        payload = {"code": 0, "msg": "ok", "data": {"knowledge_list": [], "is_end": True}}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)) as m:
            client().list_dir("KB", "F1")
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/openapi/wiki/v1/get_knowledge_list"))
        self.assertEqual(req.get_header("Ima-openapi-clientid"), FAKE_ID)
        self.assertEqual(json.loads(req.data.decode("utf-8"))["folder_id"], "F1")

    def test_pagination(self):
        p1 = FakeResp({"code": 0, "msg": "", "data": {"knowledge_list": [{"title": "a"}], "is_end": False, "next_cursor": "c2"}})
        p2 = FakeResp({"code": 0, "msg": "", "data": {"knowledge_list": [{"title": "b"}], "is_end": True}})
        with mock.patch("urllib.request.urlopen", side_effect=[p1, p2]):
            self.assertEqual([x["title"] for x in client().list_dir("KB")], ["a", "b"])

    def test_non_retryable_error_has_no_secret(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResp({"code": 110030, "msg": "无权限", "data": {}})) as m:
            with self.assertRaises(ImaError) as ctx:
                client().search("KB", "x")
        self.assertEqual(m.call_count, 1)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_retryable_error_retries(self):
        bad = FakeResp({"code": 110010, "msg": "net", "data": {}})
        good = FakeResp({"code": 0, "msg": "ok", "data": {"info_list": [], "is_end": True}})
        with mock.patch("urllib.request.urlopen", side_effect=[bad, good]), mock.patch("time.sleep"):
            self.assertEqual(client().search("KB", "x"), [])

    def test_call_log_has_no_credentials(self):
        c = client()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp({"code": 0, "msg": "ok", "data": {}})):
            c.media_info("M1")
        dumped = json.dumps([r.__dict__ for r in c.call_log])
        self.assertNotIn(FAKE_KEY, dumped)
        self.assertNotIn(FAKE_ID, dumped)

    def test_empty_media_id_rejected(self):
        with self.assertRaises(ImaError):
            client().media_info("")


if __name__ == "__main__":
    unittest.main()
