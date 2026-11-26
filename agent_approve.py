#!/usr/bin/env python3
"""agent-approve: a human says yes in Telegram before an AI agent publishes anything.

    agent-approve ask --title "Reply to octo/repo#12" [--file draft.md]   # prints ID
    agent-approve serve                 # long polling: handles button presses and replies
    agent-approve wait ID [--timeout 3600]
    agent-approve status | get ID | mark ID done|error [TEXT]

Exit codes of `wait`: 0 approved, 3 redo (feedback on stdout), 4 rejected, 5 timeout.
Only presses and replies from AA_OWNER_ID count. The approved text is frozen: `wait`
prints the path of the exact bytes the owner saw, so the agent cannot publish a newer draft.
"""
import argparse, fcntl, hashlib, html, json, os, secrets, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request

LIMIT = 3500  # Telegram allows 4096 characters; longer drafts go as a file
EXIT = {"approved": 0, "redo": 3, "rejected": 4}


def hidden(text):
    """Characters a chat shows differently or not at all: controls, bidi overrides, zero-width marks."""
    return sorted({f"U+{ord(c):04X}" for c in text if c not in "\n\t " and (
        unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Cn", "Zs", "Zl", "Zp")
        or 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF  # variation selectors
        or c in "\u115f\u1160\u3164\uffa0\u2800")})  # blank-looking letters


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"agent-approve: set {name}")
    return value


class Bot:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, files=None, **params):
        params = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in params.items()}
        if files:
            boundary = secrets.token_hex(16)
            body = b""
            for k, v in params.items():
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, (name, data) in files.items():
                body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                         "Content-Type: text/plain\r\n\r\n").encode() + data + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(self.base + method, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(self.base + method, urllib.parse.urlencode(params).encode())
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.load(r)["result"]
        except urllib.error.HTTPError as e:
            # the URL holds the token: never let it reach a log
            raise RuntimeError(f"Telegram {method}: HTTP {e.code}") from None


class Store:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)

    def path(self, item_id, ext="json"):
        if not item_id.isalnum():
            raise ValueError("bad id")
        return os.path.join(self.root, f"{item_id}.{ext}")

    def write(self, path, data):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def save(self, item):
        self.write(self.path(item["id"]), json.dumps(item, ensure_ascii=False, indent=1).encode())

    def load(self, item_id):
        with open(self.path(item_id)) as f:
            return json.load(f)

    def all(self):
        return [self.load(n[:-5]) for n in sorted(os.listdir(self.root)) if n.endswith(".json")]


def buttons(item):
    t = item["token"]
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ok:{item['id']}:{t}"},
                                 {"text": "✏️ Redo", "callback_data": f"redo:{item['id']}:{t}"},
                                 {"text": "🚫 Reject", "callback_data": f"no:{item['id']}:{t}"}]]}


def ask(bot, store, chat, title, draft):
    item = {"id": secrets.token_hex(4), "token": secrets.token_hex(4), "title": title, "status": "pending",
            "sha256": hashlib.sha256(draft).hexdigest(), "created": int(time.time()), "feedback": ""}
    store.write(store.path(item["id"], "draft"), draft)
    store.save(item)  # before sending: a press can arrive before sendMessage returns
    head = f"<b>{html.escape(title)}</b>\n<code>{item['id']}</code>\nsha256 <code>{item['sha256']}</code>"
    text = draft.decode("utf-8")  # strict: the owner must see the same bytes that get published
    marks = hidden(text)
    if marks:
        head += "\n⚠️ <b>Hidden characters:</b> " + ", ".join(marks[:10]) + " (sent as a file, open it to check)"
    if len(text) <= LIMIT and not marks:
        msg = bot.call("sendMessage", chat_id=chat, parse_mode="HTML", reply_markup=buttons(item),
                       text=f"{head}\n\n<pre>{html.escape(text)}</pre>")
    else:
        msg = bot.call("sendDocument", files={"document": (f"{item['id']}.txt", draft)}, chat_id=chat,
                       parse_mode="HTML", caption=head, reply_markup=buttons(item))
    item["message_id"] = msg["message_id"]
    store.save(item)
    return item["id"]


def handle(bot, store, owner, update):
    """One Telegram update. Returns the item changed, or None."""
    q = update.get("callback_query")
    if q:
        if str(q["from"]["id"]) != owner:
            bot.call("answerCallbackQuery", callback_query_id=q["id"], text="Not yours")
            return None
        try:
            action, item_id, token = q["data"].split(":")
            item = store.load(item_id)
        except (ValueError, OSError):
            bot.call("answerCallbackQuery", callback_query_id=q["id"], text="Unknown item")
            return None
        if action not in ("ok", "redo", "no") or token != item["token"] or item["status"] != "pending":
            bot.call("answerCallbackQuery", callback_query_id=q["id"], text="Already decided")
            return None
        chat = q["message"]["chat"]["id"]
        if action == "ok":
            item["status"] = "approved"
        elif action == "no":
            item["status"] = "rejected"
        else:
            item["status"] = "awaiting_feedback"
            prompt = bot.call("sendMessage", chat_id=chat, reply_to_message_id=item["message_id"],
                              text="What should change? Reply to this message.", reply_markup={"force_reply": True})
            item["prompt_id"] = prompt["message_id"]
        item["decided"] = int(time.time())
        store.save(item)
        bot.call("answerCallbackQuery", callback_query_id=q["id"], text=item["status"].replace("_", " "))
        bot.call("editMessageReplyMarkup", chat_id=chat, message_id=item["message_id"], reply_markup={"inline_keyboard": []})
        return item
    m = update.get("message")
    if m and str(m.get("from", {}).get("id")) == owner and m.get("reply_to_message"):
        reply_to = m["reply_to_message"]["message_id"]
        for item in store.all():
            if item["status"] == "awaiting_feedback" and item.get("prompt_id") == reply_to:
                item["status"], item["feedback"] = "redo", m.get("text", "")
                store.save(item)
                bot.call("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"], text="Got it, sent to the agent.")
                return item
    return None


def serve(bot, store, owner, once=False):
    lock = open(os.path.join(store.root, "serve.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("agent-approve: another serve is already running for this state directory")
    offset_file = os.path.join(store.root, "offset")
    try:
        offset = int(open(offset_file).read())
    except (OSError, ValueError):
        offset = 0
    tries = {}
    while True:
        try:
            updates = bot.call("getUpdates", offset=offset, timeout=0 if once else 50,
                               allowed_updates=["callback_query", "message"])
        except (RuntimeError, OSError) as e:
            print(f"agent-approve: {e}", file=sys.stderr)
            time.sleep(5)
            continue
        for u in updates:
            try:
                handle(bot, store, owner, u)
            except (RuntimeError, OSError) as e:
                # Telegram or disk trouble: keep the update and try it again, so a press is not lost;
                # after 5 tries it is skipped (Telegram rejects very old button presses for good)
                tries[u["update_id"]] = tries.get(u["update_id"], 0) + 1
                if tries[u["update_id"]] < 5:
                    print(f"agent-approve: update {u.get('update_id')} will be retried: {e}", file=sys.stderr)
                    if once:
                        return
                    time.sleep(5)
                    break
                print(f"agent-approve: update {u.get('update_id')} skipped after 5 tries: {e}", file=sys.stderr)
            except Exception as e:  # malformed update: skip it, one bad update must not stop the bot
                print(f"agent-approve: update {u.get('update_id')} skipped: {type(e).__name__}: {e}", file=sys.stderr)
            offset = u["update_id"] + 1
            store.write(offset_file, str(offset).encode())
        if once:
            return


def wait(store, item_id, timeout, poll=2.0):
    end = time.time() + timeout
    while True:
        item = store.load(item_id)
        if item["status"] in EXIT:
            if item["status"] == "approved":
                path = store.path(item_id, "draft")
                with open(path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() != item["sha256"]:
                        sys.exit("agent-approve: approved draft was modified after approval")
                print(path)
            elif item["status"] == "redo":
                print(item["feedback"])
            return EXIT[item["status"]]
        if time.time() >= end:
            return 5
        time.sleep(poll)


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent-approve")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask"); a.add_argument("--title", required=True); a.add_argument("--file")
    sub.add_parser("serve")
    w = sub.add_parser("wait"); w.add_argument("id"); w.add_argument("--timeout", type=int, default=3600)
    sub.add_parser("status")
    g = sub.add_parser("get"); g.add_argument("id")
    m = sub.add_parser("mark"); m.add_argument("id"); m.add_argument("result", choices=["done", "error"]); m.add_argument("text", nargs="?", default="")
    args = p.parse_args(argv)
    store = Store(os.path.expanduser(os.environ.get("AA_STATE_DIR", "~/.local/state/agent-approve")))
    if args.cmd == "wait":
        return wait(store, args.id, args.timeout)
    if args.cmd == "status":
        for item in store.all():
            print(json.dumps({k: item.get(k) for k in ("id", "title", "status", "feedback")}, ensure_ascii=False))
        return 0
    if args.cmd == "get":
        print(json.dumps({k: v for k, v in store.load(args.id).items() if k != "token"}, ensure_ascii=False, indent=1))
        return 0
    bot, chat = Bot(env("AA_TELEGRAM_TOKEN")), env("AA_CHAT_ID")
    if args.cmd == "ask":
        draft = open(args.file, "rb").read() if args.file else sys.stdin.buffer.read()
        if not draft.strip():
            sys.exit("agent-approve: empty draft")
        try:
            draft.decode("utf-8")
        except UnicodeDecodeError:
            sys.exit("agent-approve: draft is not valid UTF-8")
        print(ask(bot, store, chat, args.title, draft))
    elif args.cmd == "serve":
        serve(bot, store, env("AA_OWNER_ID"))
    elif args.cmd == "mark":
        item = store.load(args.id)
        item["result"] = args.result
        store.save(item)
        bot.call("sendMessage", chat_id=chat, reply_to_message_id=item["message_id"],
                 text=("✅ Published" if args.result == "done" else "⚠️ Failed") + (f": {args.text}" if args.text else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
