# Discord setup (5 minutes, done once)

The bot needs its own Discord application. You only need three values from
this process: the **bot token**, the **application (client) ID**, and **your
user ID**. The setup wizard (`uv run voicebot setup`) asks for all three and
validates them.

## 1. Create the application

1. Go to <https://discord.com/developers/applications> → **New Application**,
   give it a name (e.g. `voice-journal`), accept the terms.
2. Copy the **Application ID** (General Information page) — the wizard uses it
   to print your invite link.

## 2. Create the bot token

1. Left sidebar → **Bot** → **Reset Token** → copy the token immediately.
   Discord shows it once; if you lose it, reset again (and update `.env`).
2. On the same page, under **Privileged Gateway Intents**, enable
   **Message Content Intent** and save. Without this the bot cannot read
   message text/attachments and will appear to ignore you.

## 3. Invite the bot

The wizard prints an invite URL like:

```text
https://discord.com/oauth2/authorize?client_id=123...&permissions=101376&scope=bot
```

Open it, pick "Add to Server" (or just authorize it for DMs), complete the
captcha. The requested permissions are the minimum: read/send messages and
attachments — never Administrator. The bot only processes direct messages from
authorized users.

## 4. Copy your user ID

1. Discord → **Settings → Advanced → Developer Mode: ON**.
2. Right-click your own name → **Copy User ID** (a number like `123456789`).
3. Give it to the wizard as the owner ID, or put it in
   `ALLOWED_USER_IDS` (comma-separated for several owners).

## 5. Verify

```bash
uv run voicebot doctor   # token + provider key checked live
```

Then DM the bot an audio file. If it ignores you, the cause is almost always
one of: intent toggle not saved, wrong user ID in `ALLOWED_USER_IDS`, or the
bot not sharing/able to DM you (check Discord privacy settings). The bot logs
unauthorized attempts as `ignored unauthorized message user_id=...` — copy
that number into `ALLOWED_USER_IDS` (or re-run `uv run voicebot setup`).

## Rotating credentials

If a token ever leaks (chat, screenshot, commit, backup): **reset it in the
portal immediately** and update `.env` (or `/etc/voicebot/voicebot.env` on a
service install, then `systemctl restart voicebot`). Deleting the leaked copy
is not enough.
