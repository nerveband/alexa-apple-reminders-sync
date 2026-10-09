# Alexa and Apple Reminders Sync

This keeps your Alexa reminders and one Apple Reminders list in sync. It works both ways.

It runs on a Linux server next to [n8n](https://n8n.io). You do not need a Mac.

## What it does

It checks both sides every 2 minutes.

| You do this | This happens |
|---|---|
| Say "Alexa, remind me to call Mom at 5" | "Call Mom" shows up in your Apple list, due at 5 |
| Add an item with a date and time to the Apple list | Alexa reminds you at that time |
| Check off the item in Apple Reminders | The Alexa reminder goes away |
| Delete the reminder on Alexa, or Alexa marks it done | The Apple item gets checked off |
| Change the time on Alexa | The Apple item gets the new time |
| Change the name or time in Apple Reminders | The Alexa reminder changes too |

It works with your own lists. It also works with a list someone shared with you, like a family list.

## What it does not do

- Apple items with no time do not go to Alexa. Alexa needs a time.
- Items that were already in your Apple list before you started do not go to Alexa. Only new ones do.
- If you rename a reminder on Alexa, the Apple item keeps its old name. (Changing the name from the Apple side is fine.) Sending a new name to Apple can scramble the text on iPhones, so it skips that on purpose.
- Repeating Alexa reminders (daily, weekly, monthly, yearly) copy to Apple as repeating. Repeating Apple items do not copy to Alexa as repeating.
- Apple items made by this tool have no phone alert. Alexa does the alerting.

## How it works

There are two parts.

1. **A small Python service** in the `service` folder. It runs in Docker. It talks to Alexa and to iCloud.
2. **An n8n workflow** in the `n8n` folder. It tells the service to sync every 2 minutes. It warns you when something breaks. It also gives you a simple web page where you type in Apple codes.

The service uses two open source libraries:

- [alexapy](https://gitlab.com/keatontaylor/alexapy) for Alexa. Home Assistant uses it too.
- [pyicloud](https://github.com/timlaing/pyicloud) for iCloud.

Neither Amazon nor Apple supports these. If either company changes how its servers work, this can break until the libraries catch up.

## What you need

- A Linux server with Docker
- n8n running in Docker on that server
- An Amazon account with Alexa
- An Apple ID (two-factor is fine)

## Setup

### 1. Get the code

```bash
git clone https://github.com/nerveband/alexa-apple-reminders-sync.git
cd alexa-apple-reminders-sync/service
cp .env.example .env
chmod 600 .env
```

### 2. Fill in `.env`

Open `.env` in a text editor. Each line has a note that says what goes there.

To make the `BRIDGE_TOKEN`, run this and paste the result:

```bash
openssl rand -hex 32
```

### 3. Connect Alexa (one time)

```bash
docker compose build
docker compose run --rm reminders-sync python alexa_login.py
```

The script gives you a link. Open it in your web browser and sign in to Amazon. After you sign in, Amazon shows a "page not found" screen. That is normal. Copy the web address from the top of your browser and paste it into the script.

Your Amazon password is never saved. The script saves a token that lets the service read and change your reminders.

### 4. Start the service

```bash
docker compose up -d
```

### 5. Add the workflow to n8n

1. In n8n, make a new credential. Pick the type **Header Auth**.
   - Name: `Authorization`
   - Value: `Bearer ` and then your `BRIDGE_TOKEN`. Keep the space after `Bearer`.
2. Import `n8n/alexa-apple-reminders-sync.json`.
3. Open these three nodes and pick the credential you just made: **Run sync**, **Send code to server**, and **Ask Apple for a new code**.
4. Open the **Check result** node. Change `YOUR-N8N-DOMAIN` to your n8n web address.
5. If you want alerts, open the workflow settings and choose an error workflow. That workflow can send you a text, an email, or a push notification.
6. Turn the workflow on.

### 6. Connect iCloud

Apple asks for a code the first time.

1. Within 2 minutes of turning on the workflow, your iPhone or Mac shows an Apple sign-in message. Tap **Allow**. You will see a 6-digit code.
2. Open `https://YOUR-N8N-DOMAIN/form/icloud-code`.
3. Type the code and press **Submit**.

No code showed up? Open the same page, leave the box empty, and press **Submit**. Apple sends a new code.

Apple asks for a new code every so often. When that happens, your alert tells you, and you repeat these steps.

## Settings

All settings go in `service/.env`.

| Setting | What it is |
|---|---|
| `BRIDGE_TOKEN` | A long random password. n8n uses it to talk to the service. |
| `APPLE_ID` | Your Apple ID email |
| `APPLE_PASSWORD` | Your Apple ID password |
| `APPLE_LIST_NAME` | The exact name of the Apple list to sync |
| `ALEXA_DEFAULT_DEVICE` | Which Echo speaks reminders that you add on the Apple side |
| `ALEXA_EMAIL` | Your Amazon email. Only the Alexa login script uses it. |
| `N8N_NETWORK` | The Docker network your n8n container is on |

## Keeping it safe

- `.env` has your Apple password in it. Keep it on your server only, and do not commit it to git. The `.gitignore` file already skips it.
- The `data` folder has your Alexa token and your iCloud sign-in. Treat it like a password.
- The service has no public port. Only containers on the n8n network can reach it, and they need the token.
- Anyone who finds the code page can only send a code or ask for a new one. They cannot see your reminders. When iCloud is already connected, asking for a new code does nothing. To lock the page anyway, turn on Basic Auth in the **iCloud code form** node.

## When something goes wrong

See what the service is doing:

```bash
docker logs reminders-sync
```

See the result of the last sync:

```bash
docker exec reminders-sync python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8787/health').read().decode())"
```

Common problems:

- **"List not found."** Check `APPLE_LIST_NAME`. It has to match the list name exactly. The error shows every list name it found.
- **Alexa keeps failing.** Amazon may have signed you out. Run step 3 again, then `docker compose restart`.
- **"iCloud not ready: needs_2fa."** Do step 6 again.

## A note for people who build on this

pyicloud's normal update call sends the whole title again as a new text document. Apple devices then merge the old text and the new text, and the title gets scrambled. This service avoids that. When it changes an Apple item, it sends only the fields that changed. It also keeps Apple's per-field change history (the `ResolutionTokenMap`) and only updates the entries for the fields it touched. See `apple_patch` in `service/app.py`.

pyicloud also only reads your own lists. Shared lists live in a different part of iCloud, so this service finds the shared list and points pyicloud there. One more catch: the normal list query fails on shared lists, so the service reads the list's change feed instead.

## License

MIT
