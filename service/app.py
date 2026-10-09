"""reminders-sync: two-way sync between Alexa reminders and a shared Apple Reminders list.

Runs next to n8n on the same Docker network. n8n calls POST /sync on a schedule.
- Alexa side: alexapy with a stored OAuth refresh token (no Amazon password stored).
- Apple side: pyicloud CloudKit Reminders, pointed at the *shared* database zone that
  holds the family list (the list is owned by another iCloud user and shared to us).
State (links between Alexa and Apple items) lives in /data/sync.db.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from aiohttp import web
from alexapy import AlexaAPI, AlexaLogin

from pyicloud import PyiCloudService
from pyicloud.common.cloudkit import CKZoneID, CKZoneIDReq
from pyicloud.services.reminders import _reads as ck_reads
from pyicloud.services.reminders import _writes as ck_writes
from pyicloud.services.reminders.models.domain import RecurrenceFrequency
from pyicloud.services.reminders.service import RemindersService

DATA = os.environ.get("DATA_DIR", "/data")
AUTH_FILE = os.path.join(DATA, "alexa_auth.json")
DB_FILE = os.path.join(DATA, "sync.db")
TOKEN = os.environ["BRIDGE_TOKEN"]
APPLE_ID = os.environ["APPLE_ID"]
APPLE_PASSWORD = os.environ["APPLE_PASSWORD"]
LIST_NAME = os.environ.get("APPLE_LIST_NAME", "Family")
DEFAULT_DEVICE = os.environ.get("ALEXA_DEFAULT_DEVICE", "")  # serial or name for Apple -> Alexa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminders-sync")
for noisy in ("alexapy", "aiohttp.access", "pyicloud", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- state

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
          alexa_id TEXT UNIQUE, apple_id TEXT UNIQUE, origin TEXT,
          alexa_sig TEXT, apple_sig TEXT,
          alexa_missing INTEGER DEFAULT 0, apple_missing INTEGER DEFAULT 0,
          closed INTEGER DEFAULT 0, created_at INTEGER, closed_at INTEGER);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return con


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    con.commit()


S = {"alexa": None, "lock": asyncio.Lock(), "devices": {}, "devices_at": 0.0, "tz": {},
     "icloud": None, "icloud_status": "starting", "icloud_error": None, "shared": None, "list_id": None,
     "last_sync": None, "last_result": None}


# --------------------------------------------------------------------------- Alexa

def _save_auth(login, auth):
    if login.refresh_token and login.refresh_token != auth["oauth"].get("refresh_token"):
        auth["oauth"]["refresh_token"] = login.refresh_token
        tmp = AUTH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(auth, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, AUTH_FILE)


async def alexa_login(force=False):
    if S["alexa"] is not None and not force:
        return S["alexa"]
    if S["alexa"] is not None:
        await S["alexa"].close()
        S["alexa"] = None
    with open(AUTH_FILE) as f:
        auth = json.load(f)
    login = AlexaLogin("amazon.com", auth["email"], "", lambda n: os.path.join(DATA, n), False,
                       oauth=dict(auth["oauth"]), uuid=auth["uuid"])
    await login.login()
    if not login.status.get("login_successful") and not await login.test_loggedin(rebuild_session=False):
        await login.close()
        raise RuntimeError("Alexa login with stored refresh token failed; Alexa re-authorization needed")
    _save_auth(login, auth)
    S["alexa"] = login
    log.info("Alexa: logged in via refresh token")
    return login


async def alexa_devices(login):
    if time.time() - S["devices_at"] > 3600 or not S["devices"]:
        devices = await AlexaAPI.get_devices(login) or []
        S["devices"] = {d["serialNumber"]: d for d in devices if d.get("serialNumber")}
        prefs = await AlexaAPI.get_device_preferences(login) or {}
        for p in prefs.get("devicePreferences", []):
            if p.get("deviceSerialNumber") and p.get("timeZoneId"):
                S["tz"][p["deviceSerialNumber"]] = p["timeZoneId"]
        S["devices_at"] = time.time()
    return S["devices"]


def device_tz(serial):
    return S["tz"].get(serial) or next(iter(S["tz"].values()), "America/Los_Angeles")


async def alexa_reminders():
    """Return {id: notification} for all Alexa reminders. Raises on failure (never partial)."""
    for attempt in range(3):
        try:
            login = await alexa_login(force=attempt > 0)
            await alexa_devices(login)
            notes = await AlexaAPI.get_notifications(login)
        except RuntimeError:
            raise
        except Exception as exc:
            log.warning("Alexa fetch attempt %s failed: %s", attempt + 1, type(exc).__name__)
            notes = None
        if notes is not None:
            return {n["id"]: n for n in notes if n.get("type") == "Reminder" and n.get("id")}
        await asyncio.sleep(11)  # alexapy enforces a 10 s cooldown between notification calls
    raise RuntimeError("Alexa reminders unavailable after retries")


async def _alexa_call(method, uri, data=None):
    # Alexa write calls need the csrf cookie echoed back as a "csrf" header.
    login = S["alexa"]
    csrf = next((c.value for c in login.session.cookie_jar if c.key == "csrf"), None)
    headers = {"csrf": csrf or "", "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": login._headers.get("User-Agent", ""), "Origin": "https://alexa.amazon.com",
               "Referer": "https://alexa.amazon.com/spa/index.html"}
    resp = await getattr(login.session, method)(f"https://alexa.amazon.com{uri}", json=data, headers=headers, ssl=login._ssl)
    body = await resp.text()
    if resp.status >= 300:
        raise RuntimeError(f"Alexa {method.upper()} {uri}: HTTP {resp.status} {body[:200]}")
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


def pick_device():
    devs = S["devices"]
    if DEFAULT_DEVICE:
        for serial, d in devs.items():
            if DEFAULT_DEVICE in (serial, d.get("accountName")):
                return d
    for d in devs.values():  # first online Echo-type device that supports reminders
        if "REMINDERS" in (d.get("capabilities") or []) and d.get("online"):
            return d
    raise RuntimeError("No Alexa device available for new reminders (set ALEXA_DEFAULT_DEVICE)")


async def alexa_create(label, when_utc):
    dev = pick_device()
    local = when_utc.astimezone(ZoneInfo(device_tz(dev["serialNumber"])))
    now_ms = int(time.time() * 1000)
    payload = {
        "type": "Reminder", "status": "ON", "alarmTime": int(when_utc.timestamp() * 1000),
        "originalTime": local.strftime("%H:%M:00.000"), "originalDate": local.strftime("%Y-%m-%d"),
        "timeZoneId": None, "reminderIndex": None, "sound": None,
        "deviceSerialNumber": dev["serialNumber"], "deviceType": dev["deviceType"],
        "recurringPattern": None, "reminderLabel": label, "isSaveInFlight": True,
        "id": "createReminder", "isRecurring": False, "createdDate": now_ms,
    }
    res = await _alexa_call("put", "/api/notifications/createReminder", payload)
    if res.get("id"):
        return res["id"]
    # Fallback: find it by label and time
    for rid, n in (await alexa_reminders()).items():
        if n.get("reminderLabel") == label and n.get("alarmTime") == payload["alarmTime"]:
            return rid
    raise RuntimeError("Alexa create returned no id")


async def alexa_update(n, label, when_utc):
    upd = dict(n)
    tz = ZoneInfo(device_tz(n.get("deviceSerialNumber")))
    local = when_utc.astimezone(tz)
    upd.update({"reminderLabel": label, "alarmTime": int(when_utc.timestamp() * 1000),
                "originalDate": local.strftime("%Y-%m-%d"), "originalTime": local.strftime("%H:%M:00.000")})
    await _alexa_call("put", f"/api/notifications/{n['id']}", upd)


async def alexa_delete(alexa_id):
    await _alexa_call("delete", f"/api/notifications/{alexa_id}")


# --------------------------------------------------------------------------- iCloud

class SharedRemindersService(RemindersService):
    """pyicloud Reminders, but against the shared CloudKit database."""
    _SCOPE = "shared"


def _point_at_zone(zone_name, owner, zone_type="REGULAR_CUSTOM_ZONE"):
    zone = CKZoneID(zoneName=zone_name, ownerRecordName=owner, zoneType=zone_type)
    req = CKZoneIDReq(zoneName=zone_name, ownerRecordName=owner, zoneType=zone_type)
    ck_reads._REMINDERS_ZONE = zone
    ck_reads._REMINDERS_ZONE_REQ = req
    ck_writes._REMINDERS_ZONE_REQ = req


def _icloud_connect():
    api = PyiCloudService(APPLE_ID, APPLE_PASSWORD, cookie_directory=os.path.join(DATA, "icloud"))
    S["icloud"] = api
    if api.requires_2fa or api.requires_2sa:
        S["icloud_status"] = "needs_2fa"
        S["shared"] = None
        log.warning("iCloud: verification code required")
        return
    _icloud_open_list(api)


def _icloud_open_list(api):
    api._request_pcs_for_service("reminders")
    root = api.get_webservice_url("ckdatabasews")
    svc = SharedRemindersService(root, api.session, api.params)
    url = f"{root}/database/1/com.apple.reminders/production/shared/zones/list"
    resp = api.session.get(url, params=api.params)
    zones = resp.json().get("zones", [])
    found = []
    for z in zones:
        zid = z.get("zoneID", {})
        _point_at_zone(zid.get("zoneName"), zid.get("ownerRecordName"), zid.get("zoneType", "REGULAR_CUSTOM_ZONE"))
        for lst in svc.lists():
            found.append(lst.title)
            if lst.title == LIST_NAME and not lst.deleted:
                S["shared"], S["list_id"] = svc, lst.id
                S["icloud_status"], S["icloud_error"] = "ok", None
                log.info("iCloud: using shared list %r in zone %s", LIST_NAME, zid.get("zoneName"))
                return
    # Not shared with us: look in our own (private) lists.
    _point_at_zone("Reminders", None)
    own = RemindersService(root, api.session, api.params)
    for lst in own.lists():
        found.append(lst.title)
        if lst.title == LIST_NAME and not lst.deleted:
            S["shared"], S["list_id"] = own, lst.id
            S["icloud_status"], S["icloud_error"] = "ok", None
            log.info("iCloud: using own list %r", LIST_NAME)
            return
    raise RuntimeError(f"List {LIST_NAME!r} not found; lists seen: {found}")


async def icloud_ready():
    if S["icloud_status"] == "ok" and S["shared"] is not None:
        return True
    if S["icloud_status"] == "needs_2fa":
        return False
    try:
        await asyncio.to_thread(_icloud_connect)
    except Exception as exc:
        S["icloud_status"], S["icloud_error"] = "error", f"{type(exc).__name__}: {exc}"
        log.error("iCloud connect failed: %s", S["icloud_error"])
        return False
    return S["icloud_status"] == "ok"


REPLICA_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "reminders-sync/" + APPLE_ID)).upper()


def apple_patch(reminder_id, fields, modified):
    """Change only the given fields on an Apple reminder.

    pyicloud's update() re-sends the title and notes as new text documents and replaces the
    whole ResolutionTokenMap. Apple devices then merge the two text versions and garble the
    title. Here we send only the changed fields and bump only their entries in the existing
    token map, the way an Apple device does.
    """
    from pyicloud.common.cloudkit import CKModifyOperation
    svc = S["shared"]
    rn = reminder_id if reminder_id.startswith("Reminder/") else f"Reminder/{reminder_id}"
    recs = svc._raw.lookup([rn], zone_id=ck_reads._REMINDERS_ZONE_REQ).records
    rec = next(r for r in recs if getattr(r, "recordName", None) == rn)
    try:
        tmap = json.loads(rec.fields.get_value("ResolutionTokenMap") or "{}").get("map", {})
    except ValueError:
        tmap = {}
    now = time.time()
    for name in list(modified) + ["lastModifiedDate"]:
        old = tmap.get(name, {})
        tmap[name] = {"counter": int(old.get("counter", 0)) + 1, "modificationTime": now - 978307200.0,
                      "replicaID": REPLICA_ID}
    body = dict(fields)
    body["LastModifiedDate"] = {"type": "TIMESTAMP", "value": int(now * 1000)}
    body["ResolutionTokenMap"] = {"type": "STRING", "value": json.dumps({"map": tmap}, separators=(",", ":"))}
    op = CKModifyOperation(operationType="update", record=svc._writes._write_record(
        record_name=rn, record_type="Reminder", record_change_tag=rec.recordChangeTag, fields=body))
    resp = svc._raw.modify(operations=[op], zone_id=ck_reads._REMINDERS_ZONE_REQ)
    for r in resp.records:
        if getattr(r, "serverErrorCode", None):
            raise RuntimeError(f"Apple update failed: {r.serverErrorCode} {getattr(r, 'reason', '')}")


def apple_complete(reminder_id):
    apple_patch(reminder_id, {"Completed": {"type": "INT64", "value": 1},
                              "CompletionDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)}},
                ["completed", "completionDate"])


def apple_set_due(reminder_id, when_utc, tz_name):
    apple_patch(reminder_id, {"DueDate": {"type": "TIMESTAMP", "value": int(when_utc.timestamp() * 1000)},
                              "TimeZone": {"type": "STRING", "value": tz_name},
                              "AllDay": {"type": "INT64", "value": 0}},
                ["dueDate", "timeZone", "allDay"])


def apple_snapshot():
    # The compound "reminderList" query returns 502 on the shared database, so read the
    # zone's change feed from the start instead. It returns every live record in the list.
    items = {}
    for ev in S["shared"].iter_changes(since=None):
        r = ev.reminder
        if r is not None and r.list_id == S["list_id"] and not r.deleted:
            items[r.id] = r
    return items, {}


# --------------------------------------------------------------------------- sync

def a_sig(n):
    return f"{(n.get('reminderLabel') or '').strip()}|{n.get('alarmTime')}"


def p_due_ms(p):
    if p.due_date is None:
        return None
    d = p.due_date if p.due_date.tzinfo else p.due_date.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def p_sig(p):
    return f"{(p.title or '').strip()}|{p_due_ms(p)}"


def a_when(n):
    if isinstance(n.get("alarmTime"), (int, float)):
        return dt.datetime.fromtimestamp(n["alarmTime"] / 1000, tz=dt.timezone.utc)
    if n.get("originalDate") and n.get("originalTime"):
        local = dt.datetime.fromisoformat(f"{n['originalDate']}T{n['originalTime'][:8]}")
        return local.replace(tzinfo=ZoneInfo(device_tz(n.get("deviceSerialNumber")))).astimezone(dt.timezone.utc)
    return None


def a_repeat(n):
    p = str(n.get("recurringPattern") or "").upper()
    rule = ";".join((n.get("rRuleData") or {}).get("recurrenceRules") or []).upper()
    if not p and not rule:
        return None
    if "INTERVAL=" in rule and "INTERVAL=1" not in rule:
        return None
    if p == "P1D" or ("FREQ=DAILY" in rule and "BYDAY=" not in rule):
        return RecurrenceFrequency.DAILY
    if p.startswith("XXXX-WXX-") or ("FREQ=WEEKLY" in rule and rule.count(",") == 0):
        return RecurrenceFrequency.WEEKLY
    if p == "P1M" or "FREQ=MONTHLY" in rule:
        return RecurrenceFrequency.MONTHLY
    if p == "P1Y" or "FREQ=YEARLY" in rule:
        return RecurrenceFrequency.YEARLY
    return None


async def run_sync():
    out = {"alexa_to_apple": {"created": 0, "updated": 0, "completed": 0},
           "apple_to_alexa": {"created": 0, "updated": 0, "deleted": 0},
           "errors": [], "icloud": None}
    alexa = await alexa_reminders()                       # raises -> no action this cycle
    if not await icloud_ready():
        out["icloud"] = S["icloud_status"]
        out["errors"].append(f"iCloud not ready: {S['icloud_status']} {S['icloud_error'] or ''}".strip())
        return out
    out["icloud"] = "ok"
    try:
        apple, _rules = await asyncio.to_thread(apple_snapshot)
    except Exception as exc:
        S["icloud_status"], S["icloud_error"], S["shared"] = "error", f"{type(exc).__name__}: {exc}", None
        out["errors"].append(f"iCloud read failed: {S['icloud_error']}")
        return out

    con = db()
    now_ms = int(time.time() * 1000)
    baseline = int(meta_get(con, "baseline_ms", now_ms))
    if meta_get(con, "baseline_ms") is None:
        meta_set(con, "baseline_ms", baseline)
    svc = S["shared"]

    # 1) existing links: completion / deletion / edits
    for link in con.execute("SELECT rowid, * FROM links WHERE closed=0").fetchall():
        a = alexa.get(link["alexa_id"])
        p = apple.get(link["apple_id"])
        a_miss = 0 if a else link["alexa_missing"] + 1
        p_miss = 0 if p else link["apple_missing"] + 1
        con.execute("UPDATE links SET alexa_missing=?, apple_missing=? WHERE rowid=?", (a_miss, p_miss, link["rowid"]))
        a_done = (a is not None and a.get("status") != "ON") or a_miss >= 2
        p_done = (p is not None and (p.completed or p.deleted)) or p_miss >= 2
        if (a is None and a_miss < 2) or (p is None and p_miss < 2):
            continue  # wait one more cycle before trusting a disappearance
        try:
            if a_done and p_done:
                pass
            elif a_done:
                await asyncio.to_thread(apple_complete, p.id)
                out["alexa_to_apple"]["completed"] += 1
                log.info("Alexa done -> completed in Apple: %s", p.title)
            elif p_done:
                await alexa_delete(link["alexa_id"])
                out["apple_to_alexa"]["deleted"] += 1
                log.info("Apple done/deleted -> removed from Alexa: %s", a.get("reminderLabel"))
            elif a_sig(a) != link["alexa_sig"]:
                # Only the time goes to Apple. Rewriting the title would garble it (see apple_patch).
                when = a_when(a)
                if when and int(when.timestamp() * 1000) != p_due_ms(p):
                    await asyncio.to_thread(apple_set_due, p.id, when, device_tz(a.get("deviceSerialNumber")))
                    p.due_date = when
                    out["alexa_to_apple"]["updated"] += 1
                    log.info("Alexa time change -> Apple: %s", p.title)
                con.execute("UPDATE links SET alexa_sig=?, apple_sig=? WHERE rowid=?", (a_sig(a), p_sig(p), link["rowid"]))
                continue
            elif p_sig(p) != link["apple_sig"]:
                if p.due_date is not None and not p.all_day:
                    due = p.due_date if p.due_date.tzinfo else p.due_date.replace(tzinfo=dt.timezone.utc)
                    await alexa_update(a, (p.title or "").strip(), due)
                    a = dict(a, reminderLabel=(p.title or "").strip(), alarmTime=int(due.timestamp() * 1000))
                    out["apple_to_alexa"]["updated"] += 1
                    log.info("Apple edit -> Alexa: %s", p.title)
                con.execute("UPDATE links SET alexa_sig=?, apple_sig=? WHERE rowid=?", (a_sig(a), p_sig(p), link["rowid"]))
                continue
            else:
                continue
            con.execute("UPDATE links SET closed=1, closed_at=? WHERE rowid=?", (now_ms, link["rowid"]))
        except Exception as exc:
            out["errors"].append(f"link {link['alexa_id'][-8:]}: {type(exc).__name__}: {exc}"[:300])
    con.commit()

    linked_alexa = {r["alexa_id"] for r in con.execute("SELECT alexa_id FROM links")}
    linked_apple = {r["apple_id"] for r in con.execute("SELECT apple_id FROM links")}

    # 2) new Alexa reminders -> Apple
    for rid, n in alexa.items():
        if rid in linked_alexa or n.get("status") != "ON":
            continue
        try:
            label = (n.get("reminderLabel") or "").strip() or "Alexa reminder"
            when = a_when(n)
            dev = S["devices"].get(n.get("deviceSerialNumber"), {})
            notes = "Added from Alexa" + (f" ({dev.get('accountName')})" if dev.get("accountName") else "")
            try:
                p = await asyncio.to_thread(svc.create, list_id=S["list_id"], title=label, desc=notes, due_date=when,
                                            time_zone=device_tz(n.get("deviceSerialNumber")) if when else None)
            except Exception as exc:
                # pyicloud can retry a create that already went through and then report CONFLICT.
                if "CONFLICT" not in str(exc):
                    raise
                fresh, _ = await asyncio.to_thread(apple_snapshot)
                want = int(when.timestamp() * 1000) if when else None
                p = next((x for x in fresh.values() if x.id not in linked_apple and (x.title or "").strip() == label
                          and p_due_ms(x) == want and not x.completed), None)
                if p is None:
                    raise
            freq = a_repeat(n)
            if freq is not None:
                await asyncio.to_thread(svc.create_recurrence_rule, p, frequency=freq)
            con.execute("INSERT INTO links(alexa_id, apple_id, origin, alexa_sig, apple_sig, created_at) VALUES(?,?,?,?,?,?)",
                        (rid, p.id, "alexa", a_sig(n), p_sig(p), now_ms))
            con.commit()
            linked_apple.add(p.id)
            out["alexa_to_apple"]["created"] += 1
            log.info("Alexa -> Apple: %s", label)
        except Exception as exc:
            out["errors"].append(f"Alexa->Apple '{n.get('reminderLabel')}': {type(exc).__name__}: {exc}"[:300])

    # 3) new Apple items (created after sync start, with a future date + time) -> Alexa
    for pid, p in apple.items():
        if pid in linked_apple or p.completed or p.deleted or p.due_date is None or p.all_day:
            continue
        created_ms = int(p.created.timestamp() * 1000) if p.created else 0
        due_ms = p_due_ms(p)
        if created_ms <= baseline or due_ms <= now_ms:
            continue
        try:
            due = dt.datetime.fromtimestamp(due_ms / 1000, tz=dt.timezone.utc)
            aid = await alexa_create((p.title or "").strip() or "Reminder", due)
            n = {"reminderLabel": (p.title or "").strip() or "Reminder", "alarmTime": due_ms}
            con.execute("INSERT INTO links(alexa_id, apple_id, origin, alexa_sig, apple_sig, created_at) VALUES(?,?,?,?,?,?)",
                        (aid, pid, "apple", a_sig(n), p_sig(p), now_ms))
            con.commit()
            out["apple_to_alexa"]["created"] += 1
            log.info("Apple -> Alexa: %s", p.title)
        except Exception as exc:
            out["errors"].append(f"Apple->Alexa '{p.title}': {type(exc).__name__}: {exc}"[:300])
    con.close()
    return out


# --------------------------------------------------------------------------- HTTP

def authorized(request):
    return request.headers.get("Authorization", "") == f"Bearer {TOKEN}"


async def h_sync(request):
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    async with S["lock"]:
        try:
            res = await run_sync()
            status = 200
        except Exception as exc:
            res, status = {"errors": [f"{type(exc).__name__}: {exc}"], "icloud": S["icloud_status"]}, 503
            log.error("sync failed: %s", exc)
    res["ok"] = status == 200 and not res.get("errors")
    S["last_sync"], S["last_result"] = time.time(), res
    return web.json_response(res, status=status)


async def h_icloud_2fa(request):
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    code = str((await request.json()).get("code", "")).strip().replace(" ", "")
    api = S["icloud"]
    if api is None or S["icloud_status"] != "needs_2fa":
        return web.json_response({"ok": False, "error": f"iCloud is not waiting for a code (status {S['icloud_status']})"}, status=409)

    def verify():
        if not api.validate_2fa_code(code):
            return False
        if not api.is_trusted_session:
            api.trust_session()
        _icloud_open_list(api)
        return True

    try:
        ok = await asyncio.to_thread(verify)
    except Exception as exc:
        S["icloud_status"], S["icloud_error"] = "error", f"{type(exc).__name__}: {exc}"
        return web.json_response({"ok": False, "error": S["icloud_error"]}, status=500)
    if not ok:
        return web.json_response({"ok": False, "error": "code rejected"}, status=400)
    return web.json_response({"ok": True, "status": S["icloud_status"]})


async def h_icloud_login(request):
    """Start (or restart) iCloud sign-in. Apple pushes a code to trusted devices if needed."""
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if S["icloud_status"] == "ok" and S["shared"] is not None:
        return web.json_response({"status": "ok", "error": None})  # never break a working session
    S["icloud_status"], S["shared"] = "starting", None
    try:
        await asyncio.to_thread(_icloud_connect)
    except Exception as exc:
        S["icloud_status"], S["icloud_error"] = "error", f"{type(exc).__name__}: {exc}"
    return web.json_response({"status": S["icloud_status"], "error": S["icloud_error"]})


async def h_health(request):
    return web.json_response({"ok": True, "icloud": S["icloud_status"], "icloud_error": S["icloud_error"],
                              "alexa_logged_in": S["alexa"] is not None, "last_sync": S["last_sync"],
                              "last_result": S["last_result"]})


async def h_devices(request):
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    login = await alexa_login()
    devs = await alexa_devices(login)
    return web.json_response([{"serial": s, "name": d.get("accountName"), "family": d.get("deviceFamily"),
                               "online": d.get("online"), "reminders": "REMINDERS" in (d.get("capabilities") or []),
                               "tz": S["tz"].get(s)} for s, d in devs.items()])


app = web.Application()
app.add_routes([web.post("/sync", h_sync), web.post("/icloud/2fa", h_icloud_2fa), web.post("/icloud/login", h_icloud_login),
                web.get("/health", h_health), web.get("/devices", h_devices)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8787")), access_log=None)
