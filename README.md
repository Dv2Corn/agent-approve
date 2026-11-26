# agent-approve

A human says **yes in Telegram** before your AI agent publishes anything.

Your coding agent (Claude Code, Codex, Aider, a cron script) prepares a pull request, an issue
comment or a release note. Instead of posting it, it calls `agent-approve ask`. You get the draft in
Telegram with three buttons:

- ✅ **Approve**: the agent publishes exactly the bytes you saw;
- ✏️ **Redo**: the bot asks what to change, and your reply goes back to the agent;
- 🚫 **Reject**: nothing is published.

One Python file, standard library only, no server or database.

## Why

Agents are good at writing replies and small fixes, but letting them post under your name with no
review is how you end up apologising in someone else's repository. A review step in a chat you
already read on your phone is the cheapest guard there is.

## Guarantees

- Only presses and replies from `AA_OWNER_ID` count; anyone else in the chat is ignored.
- Each button carries a random per-item token: old messages and replays cannot approve a new draft.
- Approval is bound to the SHA-256 of the draft. If the file changes after you approved it,
  `wait` refuses with an error instead of returning it.
- The bot token is read from the environment only and never printed, even in HTTP errors.
- State is plain JSON files with mode `0600` in `~/.local/state/agent-approve`.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and press Start in a chat with it.
2. Save `agent_approve.py` somewhere on your `PATH` as `agent-approve` and make it executable.
3. Set the environment:

```sh
export AA_TELEGRAM_TOKEN=123456:ABC...   # from BotFather
export AA_CHAT_ID=123456789              # where drafts are sent (your user id, or a group)
export AA_OWNER_ID=123456789             # the only person whose presses count
```

4. Keep `agent-approve serve` running (it uses long polling, so no public URL is needed), for example
   with systemd:

```ini
# ~/.config/systemd/user/agent-approve.service
[Service]
EnvironmentFile=%h/.config/agent-approve.env
ExecStart=%h/.local/bin/agent-approve serve
Restart=always
[Install]
WantedBy=default.target
```

## Use from an agent or a script

```sh
id=$(agent-approve ask --title "Reply to octo/repo#12" --file reply.md)
case $(agent-approve wait "$id" --timeout 7200 > out.txt; echo $?) in
  0) gh pr comment 12 -R octo/repo --body-file "$(cat out.txt)" && agent-approve mark "$id" done ;;
  3) echo "Owner asked for changes: $(cat out.txt)" ;;   # rewrite and ask again
  4) echo "Rejected" ;;
  5) echo "No answer yet" ;;
esac
```

`wait` exit codes: `0` approved (prints the path of the frozen draft), `3` redo (prints your feedback),
`4` rejected, `5` timeout. `status` lists all items, `get ID` shows one, `mark ID done|error [text]`
replies under the draft so you see what happened to it.

Tell your agent the rule in its instructions file, for example in `CLAUDE.md` or `AGENTS.md`:

> Never post to GitHub or anywhere public directly. Write the text to a file, run
> `agent-approve ask`, then `agent-approve wait`, and publish only the file it prints.

## Limits

- One owner per state directory. Run a second copy with another `AA_STATE_DIR` for another person.
- Drafts over 3,500 characters are sent as a text file, not inline.
- `serve` must be running for presses to be recorded; presses made while it is down are picked up
  when it starts again (Telegram keeps updates for 24 hours).

## Tests

```sh
python3 -m unittest discover -s tests
```

## Коротко по-русски

ИИ-агент готовит ответ, PR или комментарий, но не публикует сам: черновик приходит вам в Telegram
с кнопками «Одобрить / Переделать / Отклонить». Публикуется ровно тот текст, который вы видели;
при «Переделать» бот спрашивает, что исправить, и передаёт ответ агенту. Один файл на Python без
зависимостей, работает через long polling — внешний адрес не нужен.

## License

MIT
