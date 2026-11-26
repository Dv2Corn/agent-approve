import hashlib, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_approve as aa

OWNER = "100"


class FakeBot:
    def __init__(self):
        self.calls, self.next_id = [], 10

    def call(self, method, files=None, **params):
        self.calls.append((method, files, params))
        self.next_id += 1
        return {"message_id": self.next_id}

    def methods(self):
        return [c[0] for c in self.calls]


def press(item, action, who=OWNER, token=None):
    return {"callback_query": {"id": "q", "from": {"id": int(who)}, "message": {"chat": {"id": 1}},
                               "data": f"{action}:{item['id']}:{token or item['token']}"}}


class AgentApproveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store, self.bot = aa.Store(self.dir), FakeBot()
        self.id = aa.ask(self.bot, self.store, "1", "Reply to <x>", b"hello & bye")
        self.item = self.store.load(self.id)

    def test_ask_escapes_html_and_shows_buttons(self):
        method, _, params = self.bot.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Reply to &lt;x&gt;", params["text"])
        self.assertIn("hello &amp; bye", params["text"])
        self.assertEqual(len(params["reply_markup"]["inline_keyboard"][0]), 3)
        self.assertEqual(oct(os.stat(self.store.path(self.id, "draft")).st_mode & 0o777), "0o600")

    def test_long_draft_goes_as_file(self):
        bot = FakeBot()
        aa.ask(bot, self.store, "1", "t", b"x" * (aa.LIMIT + 1))
        self.assertEqual(bot.methods(), ["sendDocument"])

    def test_approve_returns_frozen_draft(self):
        aa.handle(self.bot, self.store, OWNER, press(self.item, "ok"))
        self.assertEqual(aa.wait(self.store, self.id, 0), 0)

    def test_draft_changed_after_approval_is_refused(self):
        aa.handle(self.bot, self.store, OWNER, press(self.item, "ok"))
        with open(self.store.path(self.id, "draft"), "wb") as f:
            f.write(b"something else")
        with self.assertRaises(SystemExit):
            aa.wait(self.store, self.id, 0)

    def test_stranger_and_stale_presses_are_ignored(self):
        self.assertIsNone(aa.handle(self.bot, self.store, OWNER, press(self.item, "ok", who="999")))
        self.assertIsNone(aa.handle(self.bot, self.store, OWNER, press(self.item, "ok", token="bad")))
        self.assertEqual(self.store.load(self.id)["status"], "pending")
        aa.handle(self.bot, self.store, OWNER, press(self.item, "no"))
        self.assertIsNone(aa.handle(self.bot, self.store, OWNER, press(self.item, "ok")))
        self.assertEqual(aa.wait(self.store, self.id, 0), 4)

    def test_redo_collects_owner_reply_as_feedback(self):
        aa.handle(self.bot, self.store, OWNER, press(self.item, "redo"))
        prompt_id = self.store.load(self.id)["prompt_id"]
        self.assertEqual(aa.wait(self.store, self.id, 0), 5)  # still waiting for the reply
        stranger = {"message": {"from": {"id": 999}, "chat": {"id": 1}, "message_id": 50, "text": "x", "reply_to_message": {"message_id": prompt_id}}}
        self.assertIsNone(aa.handle(self.bot, self.store, OWNER, stranger))
        reply = {"message": {"from": {"id": int(OWNER)}, "chat": {"id": 1}, "message_id": 51, "text": "shorter please", "reply_to_message": {"message_id": prompt_id}}}
        aa.handle(self.bot, self.store, OWNER, reply)
        self.assertEqual(aa.wait(self.store, self.id, 0), 3)
        self.assertEqual(self.store.load(self.id)["feedback"], "shorter please")

    def test_hidden_characters_force_a_file_with_warning(self):
        bot = FakeBot()
        aa.ask(bot, self.store, "1", "t", "pay \u202eevil".encode())
        method, files, params = bot.calls[0]
        self.assertEqual(method, "sendDocument")
        self.assertIn("U+202E", params["caption"])
        for sneaky in ("\u00a0", "\u3164", "\ufe0f", "\u2028"):
            self.assertTrue(aa.hidden("a" + sneaky), sneaky)
        self.assertFalse(aa.hidden("Привет, мир! café 🙂\n\tok"))

    def test_invalid_utf8_is_refused(self):
        with self.assertRaises(UnicodeDecodeError):
            aa.ask(FakeBot(), self.store, "1", "t", b"\xff\xfe")

    def test_bad_update_is_skipped_and_offset_saved(self):
        class Once(FakeBot):
            def call(self, method, files=None, **params):
                if method == "getUpdates":
                    return [{"update_id": 7, "callback_query": {"id": "q", "from": {"id": int(OWNER)}}}, {"update_id": 8}]
                return super().call(method, files, **params)
        aa.serve(Once(), self.store, OWNER, once=True)
        self.assertEqual(open(os.path.join(self.dir, "offset")).read(), "9")

    def test_unknown_action_is_refused(self):
        self.assertIsNone(aa.handle(self.bot, self.store, OWNER, press(self.item, "hack")))
        self.assertEqual(self.store.load(self.id)["status"], "pending")

    def test_item_is_saved_before_the_message_is_sent(self):
        store = self.store
        class Peek(FakeBot):
            def call(self, method, files=None, **params):
                if method == "sendMessage":
                    self.seen = [i["id"] for i in store.all()]
                return super().call(method, files, **params)
        bot = Peek()
        new = aa.ask(bot, store, "1", "t", b"x")
        self.assertIn(new, bot.seen)

    def test_transient_error_keeps_the_update_for_retry(self):
        class Flaky(FakeBot):
            def call(self, method, files=None, **params):
                if method == "getUpdates":
                    return [{"update_id": 20, "callback_query": {"id": "q", "from": {"id": int(OWNER)}, "message": {"chat": {"id": 1}}, "data": "redo:x:y"}}]
                raise RuntimeError("Telegram answerCallbackQuery: HTTP 502")
        aa.serve(Flaky(), self.store, OWNER, once=True)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "offset")))

    def test_second_serve_is_refused(self):
        lock = open(os.path.join(self.dir, "serve.lock"), "w")
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        with self.assertRaises(SystemExit):
            aa.serve(FakeBot(), self.store, OWNER, once=True)

    def test_bad_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.store.path("../etc")

    def test_http_error_hides_token(self):
        import urllib.error, urllib.request
        bot = aa.Bot("SECRET123")
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 401, "x", {}, None))
        try:
            with self.assertRaises(RuntimeError) as e:
                bot.call("getMe")
            self.assertNotIn("SECRET123", str(e.exception))
        finally:
            urllib.request.urlopen = orig


if __name__ == "__main__":
    unittest.main()
