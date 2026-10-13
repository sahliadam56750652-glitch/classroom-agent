# The phone: reaching the PC, installing Lectern, and the checks

Phase 5e. There is no server. The API runs on this PC while it is on; the phone
keeps its own copy and syncs whenever it can reach the PC. This is how the phone
reaches it, and how to check that what it keeps is kept.

## Why Tailscale, in three lines

- An installed web app and its offline storage need HTTPS. `tailscale serve`
  gives this PC a real certificate on a private `*.ts.net` name; nothing is
  public, and `agent serve` stays bound to `127.0.0.1`.
- Offline needs no Tailscale. The installed app serves itself from the phone.
  Tailscale matters only for syncing, and the app says when the PC is not
  reachable.
- **The address is the storage.** Android files the app's snapshot, documents
  and outbox under the exact origin it was installed from. See "The one rule"
  below before choosing a name.

## The one rule: the address never changes

The phone's copy belongs to `https://<machine>.<tailnet>.ts.net`. Change either
part -- rename the machine, rename the tailnet, turn MagicDNS off -- and the app
opened from the new address is a different site to Android: it starts empty,
and the old copy is unreachable, including any writes still waiting in its
outbox. `config.yaml`'s `api.origin` records the address; Status on the phone
shows the address the app is running from. The two should always match.

**If it ever changes anyway:**

1. Do not uninstall the old app, and do not clear Chrome's site data. The old
   copy and its outbox are still on the phone.
2. Put the old name back: rename the machine (or the tailnet) back in the
   Tailscale admin console, so the old address resolves again. Open the old app
   while the PC is on; its outbox sends and it syncs. Nothing is lost.
3. Only if the old name truly cannot come back: install from the new address
   and let it sync on Wi-Fi. Everything the phone holds is rebuilt from the PC
   -- the snapshot and every document come from it. What cannot be rebuilt is
   only what the old copy had not sent yet: Status on the old app lists it under
   "waiting to send" and "Not sent", and it has to be redone by hand on the new
   one. Then set `api.origin` to the new address and restart `agent serve`.

## On the PC, once

1. Install Tailscale for Windows from https://tailscale.com/download and sign
   in.
2. In the admin console (https://login.tailscale.com/admin/machines), open this
   machine's menu:
   - **Edit machine name** to `lectern`, or whatever it will be forever.
   - **Disable key expiry**, so the PC does not drop off the tailnet every few
     months until someone notices the phone stopped syncing.
3. On the console's **DNS** page: MagicDNS on, and under **HTTPS Certificates**,
   Enable. Note the tailnet name shown there (`tailXXXX.ts.net`). Do not rename
   it later.
4. In a terminal:

   ```
   tailscale serve --bg http://127.0.0.1:8000
   tailscale serve status
   ```

   `--bg` keeps it across reboots. Status should print
   `https://lectern.tailXXXX.ts.net` proxying to `http://127.0.0.1:8000`.
5. In `config.yaml`:

   ```yaml
   api:
     secure_cookie: true
     origin: https://lectern.tailXXXX.ts.net
   ```

6. Start `agent serve` at logon, like the bot. In the Startup folder
   (`Win+R`, `shell:startup`), next to `classroom-agent-bot.vbs`, create
   `classroom-agent-serve.vbs`:

   ```vbs
   Set s = CreateObject("WScript.Shell")
   s.CurrentDirectory = "C:\dev\classroom-agent"
   s.Run """C:\dev\classroom-agent\.venv\Scripts\pythonw.exe"" -m agent.cli serve", 0, False
   ```

   Double-click it once to start it now.
7. On the PC, open `https://lectern.tailXXXX.ts.net`, sign in with
   `WEB_API_TOKEN` from `.env`, and check that Today appears. If sign-in loops
   back to the sign-in screen, `api.origin` does not match the address in the
   bar.

## LibreOffice, for the decks

19 of this semester's files are PowerPoint. The reader opens PDFs, so the PC
converts them. Install LibreOffice (https://www.libreoffice.org/download/), then
run `agent render`. If `soffice` is somewhere unusual, `render.soffice` in
`config.yaml` names it. `agent run` does this from then on. The first run's size
goes in PLAN.md: that is what the phone downloads.

## On the phone

See the step list in PLAN.md's 5e entry for the first install; it is also what
to repeat after a factory reset. The short form:

1. Tailscale app, signed in to the same account, connected.
2. Chrome to `https://lectern.tailXXXX.ts.net`, sign in.
3. Install from Chrome's menu. Check whether it installed as an app (it appears
   in Android's app list and app info) or only as a shortcut.
4. Open the installed app on Wi-Fi with the PC on and wait for Status to say all
   documents are held.
5. Status, "On this phone": the storage line must say **Kept**.
6. Airplane mode, then open three documents, one of them a converted deck. Mark
   one read and skip one: both say "waiting to send".
7. Airplane mode off, Tailscale on, PC on: Status says nothing is waiting, and
   Today has moved on.
