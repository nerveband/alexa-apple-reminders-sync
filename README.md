# Alexa and Apple Reminders Sync

This keeps Alexa and one Apple Reminders list in sync. It works both ways.

It covers Alexa reminders, the Alexa To-Do list, and the Alexa Shopping list. It can also sort new items into your list's sections and add "remind me when I arrive" alerts for stores.

It runs on a Linux server next to [n8n](https://n8n.io). You do not need a Mac.

## What it does

It checks both sides every 2 minutes.

| You do this | This happens |
|---|---|
| Say "Alexa, remind me to call Mom at 5" | "Call Mom" shows up in your Apple list, due at 5 |
| Say "Alexa, add soy sauce to my to-do list" (or shopping list) | "soy sauce" shows up in your Apple list |
| Add an item with a date and time to the Apple list | Alexa reminds you at that time, on the Echos you pick |
| Add an item with no time to the Apple list | It shows up on your Alexa To-Do list |
| Check off an item on either side | It gets checked off on the other side |
| Delete a reminder on Alexa, or Alexa marks it done | The Apple item gets checked off |
| Change the time on Alexa | The Apple item gets the new time |
| Change the name or time in Apple Reminders | The Alexa reminder or list item changes too |

It works with your own lists. It also works with a list someone shared with you, like a family list.

### Sorting into sections (optional)

Apple lets you split a list into sections, like "Costco" or "Pharmacy". When something new comes in from Alexa, this tool can put it in the right section for you.

- If you have filed the same item before, it uses your last choice. This is free.
- If it is new, it asks a small AI model through [OpenRouter](https://openrouter.ai). The model sees how you filed past items, so it learns your habits. It costs about $0.0001 per item.

Only items that come from Alexa get sorted. Items you add in Apple stay where you put them.

### Arrival alerts (optional)

You can save a few places, like your Costco or your home. Then:

- An item sorted into the "Costco" section gets an alert for when you arrive at your Costco.
- An item that names a place, like "pick up prescription at CVS on Main St", gets an alert for the closest match to your home. The tool looks it up on OpenStreetMap for free.
- "When I get home" items get an alert for your home.

## What it does not do

- Items that were already in your Apple list before you started do not go to Alexa. Only new ones do.
- If you rename a reminder on Alexa, the Apple item keeps its old name. (Changing the name from the Apple side is fine.) Sending a new name to Apple can scramble the text on iPhones, so it skips that on purpose.
- Repeating Alexa reminders (daily, weekly, monthly, yearly) copy to Apple as repeating. Repeating Apple items do not copy to Alexa as repeating.
- Apple items made by this tool have no timed phone alert. Alexa does the alerting. Arrival alerts are the exception.

## How it works

There are two parts.

1. **A small Python service** in the `service` folder. It runs in Docker. It talks to Alexa, to iCloud, and (if you want sorting) to OpenRouter.
2. **An n8n workflow** in the `n8n` folder. It tells the service to sync every 2 minutes. It warns you when something breaks. It also gives you a simple web page where you type in Apple codes.

The service uses two open source libraries:

- [alexapy](https://gitlab.com/keatontaylor/alexapy) for Alexa. Home Assistant uses it too.
- [pyicloud](https://github.com/timlaing/pyicloud) for iCloud.

Neither Amazon nor Apple supports these. If either company changes how its servers work, this can break until the fixes catch up.

## What you need

- A Linux server with Docker
- n8n running in Docker on that server
- An Amazon account with Alexa
- An Apple ID (two-factor is fine)
- Optional: an OpenRouter account, for sorting

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

Your Amazon password is never saved. The script saves a token that lets the service read and change your reminders and lists.

### 4. Save your places (optional)

Skip this if you do not want arrival alerts.

```bash
cp places.example.json data/places.json
```

Edit `data/places.json`:

- `home` is your home address and its map position.
- Each place has a name, an address, a map position (`lat` and `lon`), and a `radius` in meters.
- `sections` lists the section names that should get an alert for this place.
- `aliases` are other names you might say for it.

To find `lat` and `lon`, search for the place on [openstreetmap.org](https://www.openstreetmap.org), right-click it, and choose "Show address". The two numbers are the position.

### 5. Start the service

```bash
docker compose up -d
```

### 6. Add the workflow to n8n

1. In n8n, make a new credential. Pick the type **Header Auth**.
   - Name: `Authorization`
   - Value: `Bearer ` and then your `BRIDGE_TOKEN`. Keep the space after `Bearer`.
2. Import `n8n/alexa-apple-reminders-sync.json`.
3. Open these three nodes and pick the credential you just made: **Run sync**, **Send code to server**, and **Ask Apple for a new code**.
4. Open the **Check result** node. Change `YOUR-N8N-DOMAIN` to your n8n web address.
5. If you want alerts, open the workflow settings and choose an error workflow. That workflow can send you a text, an email, or a push notification.
6. Turn the workflow on.

### 7. Connect iCloud

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
| `ALEXA_DEVICES` | Which Echos speak reminders that you add on the Apple side. Names or serial numbers, separated by commas. |
| `ALEXA_LISTS` | Which Alexa lists to sync: `TODO`, `SHOP`, or both |
| `OPENROUTER_API_KEY` | Optional. Turns on AI sorting into sections. |
| `SORT_MODEL` | Which OpenRouter model sorts items. The default is cheap and fast. |
| `ALEXA_EMAIL` | Your Amazon email. Only the Alexa login script uses it. |
| `N8N_NETWORK` | The Docker network your n8n container is on |

Tip: if you rename your Echos often, use serial numbers in `ALEXA_DEVICES`. You can see them in the service's `/devices` page (see below).

## Keeping it safe

- `.env` has your Apple password and your OpenRouter key in it. Keep it on your server only, and do not commit it to git. The `.gitignore` file already skips it.
- The `data` folder has your Alexa token, your iCloud sign-in, and your places. Treat it like a password.
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

See your Echos and their serial numbers:

```bash
docker exec reminders-sync python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8787/devices', headers={'Authorization': 'Bearer ' + os.environ['BRIDGE_TOKEN']}); print(urllib.request.urlopen(r).read().decode())"
```

Common problems:

- **"List not found."** Check `APPLE_LIST_NAME`. It has to match the list name exactly. The error shows every list name it found.
- **Alexa keeps failing.** Amazon may have signed you out. Run step 3 again, then `docker compose restart`.
- **"iCloud not ready: needs_2fa."** Do step 7 again.
- **An Alexa reminder shows the wrong time in the Alexa app.** Check the Echo's time zone in the Alexa app. The reminder still goes off at the right moment.

Every time the service changes your sections, it saves the old section data in `data/sections-backup/` first.

## Notes for people who build on this

These are things that are not in any library yet. They are in `service/app.py`.

- **Shared lists.** pyicloud only reads your own lists. Shared lists live in a different part of iCloud, so the service finds the shared list and points pyicloud there. The normal list query fails on shared lists, so it reads the list's change feed instead.
- **Safe updates.** pyicloud's normal update call sends the whole title again as a new text document. Apple devices then merge the old text and the new text, and the title gets scrambled. This service sends only the fields that changed. It also keeps Apple's per-field change history (the `ResolutionTokenMap`) and only bumps the entries it touched. See `apple_patch`.
- **Sections.** Apple stores which item is in which section as one JSON file attached to the list (`MembershipsOfRemindersInSectionsAsData`), plus a checksum and a change counter. The service uploads a new file, sets the checksum to the SHA-512 of that file, and bumps the counter. The checksum field is encrypted text, so it is sent as `STRING` with `isEncrypted: true`. See `write_sections`. The [remi](https://github.com/mattheworiordan/remi) project figured out much of this format first.
- **Arrival alerts.** pyicloud's `add_location_trigger` sends everything in one batch. iCloud rejects that on shared lists. The service creates the alarm, then the location trigger, then links it to the reminder, one step at a time. See `add_arrival_alert`.
- **Alexa writes.** Changing Alexa reminders needs the `csrf` cookie sent back as a `csrf` header. The To-Do and Shopping lists use the same API as the Alexa app (`alexashoppinglists/api/v2`), which [alexa-remote](https://github.com/Apollon77/alexa-remote) documents.

## License

MIT
