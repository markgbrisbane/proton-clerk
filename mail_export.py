#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""
mail_export.py - find email chains in a Proton Mail mailbox (through Proton
Mail Bridge) and export each chain to a PDF, with a searchable index.

Run it with:   sh run.sh            (see README.md)

The steps, in the order main() calls them:

  1. load_config()        read config.yaml
  2. sync_from_bridge()   download new messages into ./cache and index them
                          (or index_eml_folder() if source is "folder")
  3. build_chains()       group messages into chains using their reply headers
  4. find_matches()       apply the keyword / people / date rules to each chain
  5. export_chain()       write one PDF per chain (+ attachments, originals)
  6. write_index()        index.html, index.csv, index.json

Only the standard library plus PyYAML is used. PDFs are printed by a
Chromium-family browser (Chrome, Edge, Brave...) running invisibly.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import difflib
import email
import email.policy
import email.utils
import getpass
import html
import imaplib
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
INDEX_VERSION = 1                 # bump to force the search index to be rebuilt
imaplib._MAXLINE = 50_000_000     # big mailboxes produce very long UID lists

DEFAULTS = {
    "keywords": [],
    "keyword_options": {"whole_words": True, "search_in": ["subject", "body"]},
    "senders": [],
    "look_for_people_in": ["from"],
    "date_ranges": [],
    "limit_to_date_ranges": False,
    "combine": "any",
    "source": "bridge",
    "bridge": {
        "host": "127.0.0.1",
        "imap_port": 1143,
        "security": "starttls",
        "folders": ["All Mail"],
        "download_since": None,
        "download_before": None,
    },
    "eml_folder": None,
    "output_folder": "exports",
    "timezone": None,
    "pdf": {"paper": "A4", "body": "html", "browser_path": None,
            "browser_args": [], "parallel": 4},
    "save_attachments": True,
    "copy_originals": True,
    "link_by_subject": False,
    "open_when_done": True,
}

BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
]


def die(message: str):
    print(f"\nSTOPPED: {message}\n", file=sys.stderr)
    sys.exit(1)


# ───────────────────────────── 1. configuration ─────────────────────────────

def merge_config(defaults: dict, given: dict, where: str = "", file: str = "config.yaml") -> dict:
    """Overlay the user's settings on the defaults, flagging misspelt names."""
    out = dict(defaults)
    for key, value in (given or {}).items():
        if key not in defaults:
            close = difflib.get_close_matches(str(key), list(defaults), n=1)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            die(f"{file} has an unknown setting '{where}{key}'.{hint}")
        if isinstance(defaults[key], dict):
            if value is not None and not isinstance(value, dict):
                die(f"'{where}{key}' in {file} should be a group of indented settings.")
            out[key] = merge_config(defaults[key], value, f"{where}{key}.", file)
        elif isinstance(defaults[key], list):
            out[key] = [] if value is None else value if isinstance(value, list) else [value]
        else:
            out[key] = value
    return out


def as_date(value, setting: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        die(f"'{value}' in {setting} is not a date. Write dates like 2026-03-01.")


def load_config(path: Path) -> dict:
    if not path.exists():
        die(f"Can't find {path.name}. It should sit next to mail_export.py.")
    try:
        given = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as err:
        die(f"{path.name} couldn't be read. Check the indentation, and put quotes around any\n"
            f"value that starts with @ or * or contains a colon. Details:\n{err}")
    cfg = merge_config(DEFAULTS, given, file=path.name)

    ranges = []
    for item in cfg["date_ranges"]:
        if not isinstance(item, dict) or "from" not in item:
            die("Each entry under date_ranges needs a 'from' (and usually a 'to') date.")
        start = as_date(item.get("from"), "date_ranges")
        end = as_date(item.get("to"), "date_ranges") or start
        if end < start:
            die(f"date_ranges: {start} to {end} ends before it starts.")
        ranges.append((start, end))
    cfg["date_ranges"] = ranges
    tidy = lambda values: [t for t in (str(v).strip(" “”‘’") for v in values) if t]
    cfg["keywords"] = tidy(cfg["keywords"])       # also drops curly quotes added by a text editor
    cfg["senders"] = tidy(cfg["senders"])

    places = [p.lower() for value in cfg["look_for_people_in"] for p in re.split(r"[,\s]+", str(value)) if p]
    for place in places:
        if place not in PEOPLE_PLACES:
            hint = (" Bcc can't be searched." if place == "bcc" else "")
            die(f"'{place}' in look_for_people_in isn't one of from, to or cc.{hint}\n"
                "Write it like this:   look_for_people_in: [from, to, cc]")
    if not places:
        die("look_for_people_in is empty. Write it like this:   look_for_people_in: [from, to, cc]")
    cfg["look_for_people_in"] = [p for p in PEOPLE_PLACES if p in places]
    if cfg["limit_to_date_ranges"] is None:
        cfg["limit_to_date_ranges"] = False
    if not isinstance(cfg["limit_to_date_ranges"], bool):
        die(f"'limit_to_date_ranges' must be either true or false, not '{cfg['limit_to_date_ranges']}'.")
    for key in ("download_since", "download_before"):
        cfg["bridge"][key] = as_date(cfg["bridge"][key], f"bridge.{key}")

    if cfg["combine"] not in ("any", "all"):
        die("'combine' must be either any or all.")
    if cfg["source"] not in ("bridge", "folder"):
        die("'source' must be either bridge or folder.")
    if not (cfg["keywords"] or cfg["senders"] or cfg["date_ranges"]):
        die("config.yaml has no keywords, senders or date_ranges, so there is nothing to look for.")
    if cfg["limit_to_date_ranges"] and not cfg["date_ranges"]:
        die("limit_to_date_ranges is true but date_ranges is empty, so there are no dates to limit to.\n"
            "Fill in date_ranges, or set limit_to_date_ranges: false.")
    if cfg["limit_to_date_ranges"] and not (cfg["keywords"] or cfg["senders"]):
        die("limit_to_date_ranges is true but there are no keywords or senders for the dates to limit.\n"
            "Add a keyword or a person, or set limit_to_date_ranges: false.")
    try:
        cfg["tz"] = ZoneInfo(cfg["timezone"]) if cfg["timezone"] else None
    except Exception:
        die(f"Unknown timezone '{cfg['timezone']}'. Use a name like Australia/Perth.")
    return cfg


def load_credentials(path: Path) -> tuple[str, str]:
    """Read the Bridge login from bridge.env, asking for it on the first run."""
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    user, password = values.get("BRIDGE_USERNAME"), values.get("BRIDGE_PASSWORD")
    if user and password:
        return user, password
    if not sys.stdin.isatty():
        die(f"Put BRIDGE_USERNAME and BRIDGE_PASSWORD in {path.name}.")
    print("\nFirst-time setup. Open Proton Mail Bridge, click your account, and look\n"
          "under 'Mailbox details'. Use the username and password shown THERE\n"
          "(the Bridge password is not your normal Proton password).\n")
    user = input("Bridge username: ").strip()
    password = getpass.getpass("Bridge password (it stays hidden as you paste, that's normal): ").strip()
    path.write_text(f"BRIDGE_USERNAME={user}\nBRIDGE_PASSWORD={password}\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"Saved to {path.name}.\n")
    return user, password


# ───────────────────────────── reading one email ─────────────────────────────

def parse_eml(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def header(msg, name: str) -> str:
    try:
        return " ".join(str(msg.get(name, "")).split())
    except Exception:           # malformed header
        return ""


def addresses(msg, name: str) -> list[tuple[str, str]]:
    """[(display name, email address), ...] for a From/To/Cc header."""
    found = []
    try:
        for value in msg.get_all(name, []):
            for addr in getattr(value, "addresses", ()):
                if addr.addr_spec:
                    found.append((addr.display_name or "", addr.addr_spec))
    except Exception:
        pass
    if not found:
        try:
            raw = [str(v) for v in msg.get_all(name, [])]
            found = [(n, a) for n, a in email.utils.getaddresses(raw) if a]
        except Exception:
            pass
    return found


def message_time(msg) -> float | None:
    """When the message was sent, as a UTC timestamp."""
    candidates = [header(msg, "Date")]
    try:
        candidates += [str(r).rsplit(";", 1)[-1] for r in msg.get_all("Received", [])]
    except Exception:
        pass
    for text in candidates:
        try:
            when = email.utils.parsedate_to_datetime(text.strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            return when.timestamp()
        except Exception:
            continue
    return None


def part_text(part) -> str:
    if part is None:
        return ""
    try:
        return part.get_content()
    except Exception:           # unknown or broken character set: guess
        data = part.get_payload(decode=True) or b""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("cp1252", "replace")


def bodies(msg):
    """(plain part, html part) - either can be None."""
    try:
        return (msg.get_body(preferencelist=("plain",)),
                msg.get_body(preferencelist=("html",)))
    except Exception:
        return None, None


def split_parts(msg, html_body: str):
    """Sort the non-body parts into real attachments and inline images."""
    plain, rich = bodies(msg)
    attachments, inline = [], {}
    for part in msg.walk():
        if part.is_multipart() or part is plain or part is rich:
            continue
        content_id = str(part.get("Content-ID", "")).strip().strip("<>")
        if content_id and f"cid:{content_id}" in html_body:
            inline[content_id] = part
        elif part.get_filename() or part.get_content_disposition() == "attachment":
            attachments.append(part)
    return attachments, inline


class _TextOnly(HTMLParser):
    BREAKS = {"br", "p", "div", "td", "th", "tr", "li", "table",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    HIDDEN = {"script", "style", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag in self.BREAKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self.hidden:
            self.hidden -= 1
        elif tag in self.BREAKS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextOnly()
    try:
        parser.feed(markup)
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return "".join(parser.chunks)


def index_fields(msg) -> dict:
    """The searchable facts about one message, stored in the cache index."""
    plain, rich = bodies(msg)
    rich_text = part_text(rich)
    text = part_text(plain) + "\n" + html_to_text(rich_text)
    attachments, _ = split_parts(msg, rich_text)
    sender = (addresses(msg, "From") or [("", "")])[0]
    ids = lambda name: re.findall(r"<([^<>\s]+)>", header(msg, name))
    return {
        "message_id": (ids("Message-ID") or [""])[0],
        "refs": json.dumps(ids("In-Reply-To") + ids("References")),
        "date_ts": message_time(msg),
        "from_name": sender[0],
        "from_addr": sender[1],
        "to_addrs": json.dumps(addresses(msg, "To")),
        "cc_addrs": json.dumps(addresses(msg, "Cc")),
        "subject": header(msg, "Subject"),
        "body_text": " ".join(text.split()),
        "attachment_names": json.dumps([a.get_filename() or "unnamed" for a in attachments]),
    }


# ───────────────────────── 2. local cache and syncing ─────────────────────────

def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:
        db.execute("DROP TABLE IF EXISTS messages")    # rebuilt from the saved .eml files
        db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        key TEXT PRIMARY KEY, source TEXT, folder TEXT, path TEXT,
        message_id TEXT, refs TEXT, date_ts REAL, from_name TEXT, from_addr TEXT,
        to_addrs TEXT, cc_addrs TEXT, subject TEXT, body_text TEXT, attachment_names TEXT)""")
    return db


def add_to_index(db, key: str, source: str, folder: str, path: Path, raw: bytes):
    row = {"key": key, "source": source, "folder": folder, "path": str(path)}
    try:
        row.update(index_fields(parse_eml(raw)))
    except Exception as err:
        print(f"   (skipped an unreadable message: {path.name}: {err})")
        return
    columns = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    db.execute(f"INSERT OR REPLACE INTO messages ({columns}) VALUES ({marks})", row)


def connect_bridge(cfg: dict, user: str, password: str) -> imaplib.IMAP4:
    bridge = cfg["bridge"]
    host, port = bridge["host"], int(bridge["imap_port"])
    context = ssl.create_default_context()
    if host in ("127.0.0.1", "localhost", "::1"):
        # Bridge uses a self-signed certificate. The connection never leaves
        # this computer, so the certificate check is skipped for local hosts only.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        if bridge["security"] == "ssl":
            imap = imaplib.IMAP4_SSL(host, port, ssl_context=context)
        else:
            imap = imaplib.IMAP4(host, port)
            imap.starttls(context)
    except (OSError, imaplib.IMAP4.error) as err:
        die(f"Couldn't reach Proton Mail Bridge at {host}:{port} ({err}).\n"
            "Open the Bridge app, check it is signed in and finished syncing, and check\n"
            "that the IMAP port and connection mode in Bridge's settings match config.yaml.")
    try:
        imap.login(user, password)
    except imaplib.IMAP4.error:
        die("Bridge rejected the username or password. Delete bridge.env and run again,\n"
            "copying both from 'Mailbox details' in the Bridge app (not your Proton password).")
    return imap


def uid_search(imap, *criteria) -> set[int]:
    status, data = imap.uid("SEARCH", None, *criteria)
    if status != "OK":
        die(f"Bridge refused a search request: {data}")
    return {int(uid) for uid in (data[0] or b"").split()}


def imap_date(day: dt.date) -> str:
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{day.day:02d}-{months[day.month - 1]}-{day.year}"


def sync_from_bridge(cfg: dict, db, cache: Path):
    """Download messages that aren't in the cache yet. Nothing in the mailbox is changed."""
    user, password = load_credentials(HERE / "bridge.env")
    imap = connect_bridge(cfg, user, password)
    bridge = cfg["bridge"]
    for folder in bridge["folders"]:
        quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
        status, _ = imap.select(quoted, readonly=True)
        if status != "OK":
            _, listing = imap.list()
            names = [re.split(r' "[^"]*" ', line.decode("utf-8", "replace"), maxsplit=1)[-1]
                     for line in listing or [] if line]
            die(f"Bridge has no folder called '{folder}'. It offers: {', '.join(names)}")
        validity = int(imap.response("UIDVALIDITY")[1][0] or 0)
        on_server = uid_search(imap, "ALL")
        prefix = f"imap:{folder}:{validity}:"

        # Forget cached messages that have since been deleted from the mailbox.
        cached = db.execute("SELECT key, path FROM messages WHERE source='imap' AND folder=?", (folder,)).fetchall()
        for row in cached:
            current = row["key"].startswith(prefix) and int(row["key"].rsplit(":", 1)[1]) in on_server
            if not current:
                db.execute("DELETE FROM messages WHERE key=?", (row["key"],))
                Path(row["path"]).unlink(missing_ok=True)

        wanted = on_server
        if bridge["download_since"] or bridge["download_before"]:
            criteria = []
            if bridge["download_since"]:
                criteria += ["SINCE", imap_date(bridge["download_since"])]
            if bridge["download_before"]:
                criteria += ["BEFORE", imap_date(bridge["download_before"])]
            wanted = uid_search(imap, *criteria)

        indexed = {r["key"] for r in db.execute("SELECT key FROM messages WHERE source='imap' AND folder=?", (folder,))}
        todo = sorted(uid for uid in wanted if f"{prefix}{uid}" not in indexed)
        folder_dir = cache / "eml" / slug(folder) / str(validity)
        folder_dir.mkdir(parents=True, exist_ok=True)
        print(f"'{folder}': {len(on_server)} messages in the mailbox, {len(todo)} new to download.")
        for count, uid in enumerate(todo, 1):
            path = folder_dir / f"{uid}.eml"
            if path.exists():
                raw = path.read_bytes()
            else:
                status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((item[1] for item in data or [] if isinstance(item, tuple)), None)
                if status != "OK" or raw is None:
                    print(f"   (couldn't download message {uid}, skipping it)")
                    continue
                partial = path.with_suffix(".part")
                partial.write_bytes(raw)
                partial.rename(path)
            add_to_index(db, f"{prefix}{uid}", "imap", folder, path, raw)
            if count % 100 == 0 or count == len(todo):
                db.commit()
                print(f"   downloaded {count} of {len(todo)}", end="\r", flush=True)
        if todo:
            print()
        db.commit()
    imap.logout()


def index_eml_folder(cfg: dict, db):
    """Alternative source: a folder of .eml files (e.g. from the Proton Mail Export Tool)."""
    if not cfg["eml_folder"]:
        die("source is 'folder' but eml_folder is empty in config.yaml.")
    root = Path(str(cfg["eml_folder"])).expanduser()
    if not root.is_dir():
        die(f"eml_folder '{root}' doesn't exist.")
    files = {f"file:{p}": p for p in sorted(root.rglob("*.eml"))}
    indexed = {r["key"] for r in db.execute("SELECT key FROM messages WHERE source='folder' AND folder=?", (str(root),))}
    for key in indexed - set(files):
        db.execute("DELETE FROM messages WHERE key=?", (key,))
    todo = [key for key in files if key not in indexed]
    print(f"'{root}': {len(files)} .eml files, {len(todo)} new to index.")
    for count, key in enumerate(todo, 1):
        add_to_index(db, key, "folder", str(root), files[key], files[key].read_bytes())
        if count % 500 == 0:
            db.commit()
    db.commit()


def load_messages(cfg: dict, db) -> list[dict]:
    if cfg["source"] == "bridge":
        folders = cfg["bridge"]["folders"]
        marks = ",".join("?" * len(folders))
        rows = db.execute(f"SELECT * FROM messages WHERE source='imap' AND folder IN ({marks})", folders)
    else:
        root = str(Path(str(cfg["eml_folder"])).expanduser())
        rows = db.execute("SELECT * FROM messages WHERE source='folder' AND folder=?", (root,))
    messages, seen = [], set()
    for row in rows:
        m = dict(row)
        if m["message_id"] and m["message_id"] in seen:     # same email filed in two folders
            continue
        seen.add(m["message_id"])
        for column in ("refs", "to_addrs", "cc_addrs", "attachment_names"):
            m[column] = json.loads(m[column] or "[]")
        messages.append(m)
    return messages


# ───────────────────────────── 3. building chains ─────────────────────────────

BRIDGE_REF_SUFFIX = "@protonmail.internalid"
REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|tr|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)


def base_subject(subject: str) -> str:
    return REPLY_PREFIX.sub("", subject or "").strip()


def build_chains(messages: list[dict], link_by_subject: bool) -> list[list[dict]]:
    """Group messages that reply to one another. Each chain is sorted oldest first."""
    parent: dict[str, str] = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        parent[find(a)] = find(b)

    by_time = sorted(messages, key=lambda m: m["date_ts"] or 0)
    for m in by_time:
        m["node"] = m["message_id"] or m["key"]
        for ref in m["refs"]:
            union(m["node"], ref)

    if link_by_subject:
        # Optional: a "Re: X" that lost its reply headers joins the earliest chain about "X".
        first_with_subject: dict[str, str] = {}
        for m in by_time:
            base = base_subject(m["subject"]).lower()
            is_reply = base != (m["subject"] or "").strip().lower()
            # Bridge adds its own <...@protonmail.internalid> reference to every message,
            # so only references from the sender count as reply headers here.
            real_refs = [r for r in m["refs"] if not r.endswith(BRIDGE_REF_SUFFIX)]
            if base and is_reply and not real_refs and base in first_with_subject:
                union(m["node"], first_with_subject[base])
            if base:
                first_with_subject.setdefault(base, m["node"])

    chains: dict[str, list[dict]] = {}
    for m in by_time:
        chains.setdefault(find(m["node"]), []).append(m)
    return list(chains.values())


# ───────────────────────────── 4. matching rules ─────────────────────────────

def keyword_pattern(keyword: str, whole_words: bool) -> re.Pattern:
    """plain words, "a phrase", wild* cards, or /a regular expression/"""
    if len(keyword) > 2 and keyword.startswith("/") and keyword.endswith("/"):
        try:
            return re.compile(keyword[1:-1], re.I | re.S)
        except re.error as err:
            die(f"The keyword {keyword} isn't a valid regular expression: {err}")
    words = [re.escape(word).replace(r"\*", r"\w*") for word in keyword.split()]
    pattern = r"\s+".join(words)
    if whole_words:
        pattern = rf"(?<!\w){pattern}(?!\w)"
    return re.compile(pattern, re.I)


PEOPLE_PLACES = ("from", "to", "cc")       # the headers look_for_people_in can name


def sender_matches(rule: str, name: str, address: str) -> bool:
    rule, name, address = rule.lower(), (name or "").lower(), (address or "").lower()
    if rule.startswith("@"):                                  # @example.com = whole domain
        return address.endswith(rule) or address.endswith("." + rule[1:])
    if "@" in rule:                                           # exact address
        return address == rule
    return rule in address or rule in name                    # part of a name or address


def find_matches(chains: list[list[dict]], cfg: dict) -> list[dict]:
    """Return the chains that satisfy the rules, oldest first, with the reasons why."""
    options = cfg["keyword_options"]
    patterns = [(k, keyword_pattern(k, options["whole_words"])) for k in cfg["keywords"]]
    fields = set(options["search_in"])
    active = [name for name, rules in (("keyword", cfg["keywords"]), ("sender", cfg["senders"]),
                                       ("date", cfg["date_ranges"])) if rules]
    places = cfg["look_for_people_in"]
    matched = []
    for messages in chains:
        kinds = set()
        for m in messages:
            reasons = []
            haystack = []
            if "subject" in fields:
                haystack.append(m["subject"] or "")
            if "body" in fields:
                haystack.append(m["body_text"] or "")
            if "attachment_names" in fields:
                haystack += m["attachment_names"]
            text = "\n".join(haystack)
            reasons += [f"keyword: {k}" for k, pattern in patterns if pattern.search(text)]
            received = [pair for place in ("to", "cc") if place in places for pair in m[f"{place}_addrs"]]
            for s in cfg["senders"]:
                if "from" in places and sender_matches(s, m["from_name"], m["from_addr"]):
                    reasons.append(f"sender: {s}")
                if any(sender_matches(s, name, address) for name, address in received):
                    reasons.append(f"recipient: {s}")
            if m["date_ts"] is not None:
                day = local_time(m["date_ts"], cfg["tz"]).date()
                reasons += [f"date: {a} to {b}" if a != b else f"date: {a}"
                            for a, b in cfg["date_ranges"] if a <= day <= b]
            m["reasons"] = reasons
            # A person found as a recipient satisfies the same rule as one found as the sender.
            kinds |= {reason.split(":")[0].replace("recipient", "sender") for reason in reasons}
        if cfg["limit_to_date_ranges"]:
            # The dates narrow the other rules instead of being a rule of their own.
            others = [k for k in active if k != "date"]
            wanted = "date" in kinds and (any if cfg["combine"] == "any" else all)(k in kinds for k in others)
        else:
            wanted = any(k in kinds for k in active) if cfg["combine"] == "any" else all(k in kinds for k in active)
        if wanted:
            reasons = sorted({r for m in messages for r in m["reasons"]})
            matched.append({"messages": messages, "reasons": reasons})
    matched.sort(key=lambda c: c["messages"][0]["date_ts"] or 0)
    return matched


# ───────────────────────────── small helpers ─────────────────────────────

def local_time(timestamp: float, tz) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone(tz)


def show_time(timestamp: float | None, tz, fmt: str = "%a %d %b %Y, %H:%M %Z") -> str:
    return local_time(timestamp, tz).strftime(fmt) if timestamp is not None else "unknown date"


def slug(text: str, limit: int = 60) -> str:
    """A filename-safe version of some text: lower-case words joined by hyphens."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w]+", "-", text).strip("-_")
    return text[:limit].rstrip("-_") or "none"


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "unnamed")
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", name).strip(". ")
    return name[-150:] or "unnamed"


def unique_path(path: Path, taken: set) -> Path:
    candidate, n = path, 2
    while str(candidate).lower() in taken or candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    taken.add(str(candidate).lower())
    return candidate


def person(name: str, address: str) -> str:
    return f"{name} <{address}>" if name else address


def size_label(count: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def tags(reasons: list[str]) -> str:
    return "".join(f'<span class="tag">{esc(r)}</span>' for r in reasons)


# ───────────────────────────── 5. exporting to PDF ─────────────────────────────

def find_browser(cfg: dict) -> str:
    chosen = cfg["pdf"]["browser_path"]
    if chosen and not Path(chosen).expanduser().exists():
        die(f"pdf.browser_path in config.yaml points to '{chosen}', which doesn't exist.")
    candidates = [chosen] if chosen else BROWSERS + [str(Path.home() / b.lstrip("/")) for b in BROWSERS]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
        if shutil.which(name):
            return shutil.which(name)
    die("PDFs are made with Google Chrome (or Edge, Brave or Chromium) and none was found.\n"
        "Install Chrome from https://www.google.com/chrome/ and run this again, or set\n"
        "pdf.browser_path in config.yaml.")


def print_to_pdf(browser: str, page: Path, pdf: Path, extra_args: list) -> bool:
    """Ask the browser to print one HTML file to PDF, invisibly and offline."""
    with tempfile.TemporaryDirectory() as profile:      # separate profile: an open Chrome is untouched
        command = [browser, "--headless", "--disable-gpu", "--no-first-run",
                   "--disable-extensions", f"--user-data-dir={profile}",
                   "--host-resolver-rules=MAP * ~NOTFOUND",          # no network access at all
                   "--no-pdf-header-footer", "--print-to-pdf-no-header",
                   *extra_args, f"--print-to-pdf={pdf}", page.as_uri()]
        try:
            subprocess.run(command, capture_output=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError):
            return False
    return pdf.exists() and pdf.stat().st_size > 0


def clean_email_html(markup: str, inline: dict) -> str:
    """Make an email's HTML safe to embed: no scripts, no page-level tags, inline images embedded."""
    markup = re.sub(r"(?is)<(script|title)\b.*?</\1\s*>", "", markup)
    markup = re.sub(r"(?is)</?(script|template|base|meta|link|iframe|frame|object|embed|html|head|body)\b[^>]*>", "", markup)
    for content_id, part in inline.items():
        data = base64.b64encode(part.get_payload(decode=True) or b"").decode("ascii")
        markup = markup.replace(f"cid:{content_id}", f"data:{part.get_content_type()};base64,{data}")
    return markup


PDF_STYLE = """
@page { size: %(paper)s; margin: 16mm 14mm 18mm;
        @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt;
                        font-family: -apple-system, Helvetica, Arial, sans-serif; color: #666; } }
body { font: 10.5pt/1.45 -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif; color: #111; margin: 0; }
.chain { border-bottom: 2px solid #111; padding-bottom: 8pt; margin-bottom: 14pt; }
.chain h1 { font-size: 16pt; margin: 0 0 4pt; }
.chain p { margin: 2pt 0; color: #444; font-size: 9pt; }
.msg { margin-bottom: 18pt; }
.meta { width: 100%%; border-collapse: collapse; background: #f3f4f6; border-left: 3px solid #4b5563;
        font-size: 9pt; break-inside: avoid; break-after: avoid; margin-bottom: 8pt; }
.meta th { text-align: left; vertical-align: top; width: 62pt; padding: 2pt 6pt; color: #555; font-weight: 600; }
.meta td { padding: 2pt 6pt; overflow-wrap: anywhere; }
.meta .n { float: right; color: #555; }
.tag { display: inline-block; border: 1px solid #b45309; color: #92400e; background: #fffbeb;
       border-radius: 3pt; padding: 0 4pt; margin: 0 3pt 2pt 0; font-size: 8pt; }
pre.plain { font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
"""

# Each HTML email sits in its own "shadow root" so its styles can't leak into
# other messages or the page furniture. Remote images are hidden (and blocked).
EMAIL_STYLE = ("<style>:host{display:block} img{max-width:100%;height:auto}"
               " img:not([src^='data:']){display:none!important} table{max-width:100%}"
               " pre{white-space:pre-wrap}</style>")

SECURITY = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:\">")


def export_chain(chain: dict, number: int, pdf_path: Path, out: Path, cfg: dict, browser: str | None) -> dict:
    """Write the PDF, attachments and originals for one chain; return its index entry."""
    tz, name = cfg["tz"], pdf_path.stem
    messages, sections, records, people = chain["messages"], [], [], {}
    all_attachments = []

    for position, m in enumerate(messages, 1):
        raw = Path(m["path"]).read_bytes()
        msg = parse_eml(raw)
        plain, rich = bodies(msg)
        rich_text = part_text(rich)
        attachments, inline = split_parts(msg, rich_text)
        sender = person(m["from_name"], m["from_addr"])
        to = [person(n, a) for n, a in m["to_addrs"]]
        cc = [person(n, a) for n, a in m["cc_addrs"]]
        for n, a in [(m["from_name"], m["from_addr"])] + m["to_addrs"] + m["cc_addrs"]:
            if a and (a.lower() not in people or (n and "<" not in people[a.lower()])):
                people[a.lower()] = person(n, a)

        saved = []
        for part in attachments:
            data = part.get_payload(decode=True) or b""
            filename = safe_filename(part.get_filename() or "unnamed")
            entry = {"name": filename, "size": len(data)}
            if cfg["save_attachments"]:
                folder = out / "attachments" / name
                folder.mkdir(parents=True, exist_ok=True)
                target = unique_path(folder / f"{position:02d}_{filename}", set())
                target.write_bytes(data)
                entry["file"] = str(target.relative_to(out))
            saved.append(entry)
        all_attachments += saved

        original = None
        if cfg["copy_originals"]:
            folder = out / "originals" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{position:02d}.eml").write_bytes(raw)
            original = str((folder / f"{position:02d}.eml").relative_to(out))

        if rich_text and cfg["pdf"]["body"] == "html":
            body = (f'<div><template shadowrootmode="open">{EMAIL_STYLE}'
                    f'{clean_email_html(rich_text, inline)}</template></div>')
        else:
            text = part_text(plain) or html_to_text(rich_text)
            body = f'<pre class="plain">{esc(text.strip() or "(no text content)")}</pre>'

        rows = [("From", esc(sender)), ("To", esc(", ".join(to)))]
        if cc:
            rows.append(("Cc", esc(", ".join(cc))))
        rows += [("Date", esc(show_time(m["date_ts"], tz))), ("Subject", esc(m["subject"]))]
        if saved:
            rows.append(("Attached", esc(", ".join(f"{a['name']} ({size_label(a['size'])})" for a in saved))))
        if m["reasons"]:
            rows.append(("Matched", tags(m["reasons"])))
        rows[0] = (rows[0][0], f'<span class="n">{position} of {len(messages)}</span>' + rows[0][1])
        meta = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
        sections.append(f'<section class="msg"><table class="meta">{meta}</table>{body}</section>')

        records.append({
            "position": position, "date": show_time(m["date_ts"], tz, "%Y-%m-%d %H:%M"),
            "from": sender, "to": to, "cc": cc, "subject": m["subject"],
            "message_id": m["message_id"], "matched": m["reasons"],
            "attachments": saved, "original": original,
            "preview": (m["body_text"] or "")[:220],
        })

    first, last = messages[0], messages[-1]
    subject = base_subject(first["subject"]) or "(no subject)"
    span = show_time(first["date_ts"], tz)
    if len(messages) > 1:
        span += " to " + show_time(last["date_ts"], tz)
    page = (f"<!doctype html><html><head><meta charset=\"utf-8\">{SECURITY}<title>{esc(subject)}</title>"
            f"<style>{PDF_STYLE % {'paper': cfg['pdf']['paper']}}</style></head><body>"
            f"<header class=\"chain\"><h1>{esc(subject)}</h1>"
            f"<p>{len(messages)} message{'s' if len(messages) != 1 else ''} &middot; {esc(span)}</p>"
            f"<p>{esc(', '.join(people.values()))}</p><p>{tags(chain['reasons'])}</p></header>"
            f"{''.join(sections)}</body></html>")

    entry = {
        "number": number, "subject": subject,
        "first_date": show_time(first["date_ts"], tz, "%Y-%m-%d %H:%M"),
        "last_date": show_time(last["date_ts"], tz, "%Y-%m-%d %H:%M"),
        "message_count": len(messages), "participants": list(people.values()),
        "matched": chain["reasons"], "attachment_count": len(all_attachments),
        "attachments_folder": f"attachments/{name}" if cfg["save_attachments"] and all_attachments else None,
        "pdf": None, "messages": records,
    }
    if browser:
        with tempfile.TemporaryDirectory() as work:
            page_file = Path(work) / "chain.html"
            page_file.write_text(page, encoding="utf-8")
            if print_to_pdf(browser, page_file, pdf_path, cfg["pdf"]["browser_args"]):
                entry["pdf"] = str(pdf_path.relative_to(out))
            else:       # keep something readable rather than losing the chain
                fallback = pdf_path.with_suffix(".html")
                fallback.write_text(page, encoding="utf-8")
                entry["pdf"] = str(fallback.relative_to(out))
                entry["pdf_failed"] = True
    return entry


# ───────────────────────────── 6. the index files ─────────────────────────────

INDEX_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Email export index</title>
<style>
body { font: 14px/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #1f2937; margin: 24px; }
h1 { font-size: 20px; margin: 0 0 4px; } .sub { color: #6b7280; margin: 0 0 16px; }
#q { font: inherit; padding: 7px 10px; width: min(480px, 100%%); border: 1px solid #9ca3af; border-radius: 6px; }
#count { color: #6b7280; margin-left: 10px; }
table { border-collapse: collapse; width: 100%%; margin-top: 14px; }
th { text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #6b7280;
     border-bottom: 2px solid #d1d5db; padding: 6px 8px; cursor: pointer; white-space: nowrap; user-select: none; }
td { border-bottom: 1px solid #e5e7eb; padding: 8px; vertical-align: top; }
tr:hover td { background: #f9fafb; }
td.num, td.date { white-space: nowrap; font-variant-numeric: tabular-nums; }
a { color: #1d4ed8; } summary { cursor: pointer; font-weight: 600; }
.preview { color: #6b7280; font-size: 13px; margin: 2px 0 0; }
.tag { display: inline-block; border: 1px solid #d97706; color: #92400e; background: #fffbeb; border-radius: 4px;
       padding: 0 6px; margin: 0 4px 3px 0; font-size: 12px; cursor: pointer; white-space: nowrap; }
ol { margin: 6px 0 0; padding-left: 22px; font-size: 13px; } li { margin-bottom: 4px; }
.people { font-size: 13px; color: #374151; max-width: 260px; overflow-wrap: anywhere; }
.warn { color: #b91c1c; font-size: 12px; }
</style></head><body>
<h1>Email export index</h1>
<p class="sub">%(summary)s</p>
<input id="q" type="search" placeholder="Filter by any word: name, keyword, date, subject..." autofocus>
<span id="count"></span>
<table><thead><tr><th>#</th><th>First email</th><th>Last email</th><th>Subject and messages</th>
<th>People</th><th>Emails</th><th>Matched because</th><th>Files</th></tr></thead>
<tbody>%(rows)s</tbody></table>
<script>
const q = document.getElementById('q'), count = document.getElementById('count');
const body = document.querySelector('tbody'), rows = [...body.rows];
function filter() {
  const words = q.value.toLowerCase().split(/\\s+/).filter(Boolean); let shown = 0;
  for (const r of rows) { const t = r.textContent.toLowerCase();
    r.hidden = !words.every(w => t.includes(w)); shown += !r.hidden; }
  count.textContent = shown + ' of ' + rows.length + ' chains';
}
q.addEventListener('input', filter);
document.querySelectorAll('.tag').forEach(t => t.addEventListener('click', () => { q.value = t.textContent; filter(); }));
document.querySelectorAll('th').forEach((th, i) => { let up = true; th.addEventListener('click', () => {
  const num = i === 0 || i === 5, val = r => r.cells[i].textContent.trim().toLowerCase();
  rows.sort((a, b) => num ? val(a) - val(b) : val(a).localeCompare(val(b))); if (!up) rows.reverse();
  up = !up; rows.forEach(r => body.appendChild(r)); }); });
filter();
</script></body></html>
"""


def link(path: str | None, label: str) -> str:
    return f'<a href="{urllib.parse.quote(path)}">{esc(label)}</a>' if path else ""


def write_index(entries: list[dict], out: Path, cfg: dict):
    criteria = {
        "keywords": cfg["keywords"], "senders": cfg["senders"],
        "look_for_people_in": cfg["look_for_people_in"], "combine": cfg["combine"],
        "date_ranges": [[str(a), str(b)] for a, b in cfg["date_ranges"]],
        "limit_to_date_ranges": cfg["limit_to_date_ranges"],
    }
    generated = dt.datetime.now().astimezone(cfg["tz"]).strftime("%Y-%m-%d %H:%M %Z")
    (out / "index.json").write_text(json.dumps(
        {"generated": generated, "criteria": criteria, "chains": entries}, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(out / "index.csv", "w", newline="", encoding="utf-8-sig") as handle:      # opens cleanly in Excel
        writer = csv.writer(handle)
        writer.writerow(["#", "First email", "Last email", "Subject", "Emails", "People",
                         "Matched because", "Attachments", "Attachment names", "PDF"])
        for e in entries:
            names = [a["name"] for m in e["messages"] for a in m["attachments"]]
            writer.writerow([e["number"], e["first_date"], e["last_date"], e["subject"], e["message_count"],
                             "; ".join(e["participants"]), "; ".join(e["matched"]),
                             e["attachment_count"], "; ".join(names), e["pdf"] or ""])

    rows = []
    for e in entries:
        items = "".join(
            f"<li>{esc(m['date'])} &middot; {esc(m['from'])}"
            + " " + tags(m["matched"])
            + (f" &middot; {link(m['original'], 'original')}" if m["original"] else "")
            + f'<div class="preview">{esc(m["preview"])}</div></li>' for m in e["messages"])
        files = [link(e["pdf"], "PDF" if not e.get("pdf_failed") else "HTML")]
        if e.get("pdf_failed"):
            files.append('<span class="warn">PDF failed</span>')
        if e["attachments_folder"]:
            files.append(link(e["attachments_folder"], f"{e['attachment_count']} attachment(s)"))
        elif e["attachment_count"]:
            files.append(f"{e['attachment_count']} attachment(s)")
        rows.append(
            f'<tr><td class="num">{e["number"]}</td><td class="date">{esc(e["first_date"])}</td>'
            f'<td class="date">{esc(e["last_date"])}</td>'
            f'<td><details><summary>{esc(e["subject"])}</summary><ol>{items}</ol></details></td>'
            f'<td class="people">{esc("; ".join(e["participants"]))}</td><td class="num">{e["message_count"]}</td>'
            f'<td>{tags(e["matched"])}</td>'
            f'<td>{"<br>".join(f for f in files if f)}</td></tr>')
    summary = f"{len(entries)} chains, {sum(e['message_count'] for e in entries)} emails. Generated {generated}."
    (out / "index.html").write_text(INDEX_PAGE % {"summary": esc(summary), "rows": "\n".join(rows)}, encoding="utf-8")


# ───────────────────────────── main ─────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export matching Proton Mail chains to PDF.")
    parser.add_argument("--config", default=str(HERE / "config.yaml"), help="settings file (default: config.yaml)")
    parser.add_argument("--offline", action="store_true", help="don't contact Bridge; search what was downloaded before")
    parser.add_argument("--dry-run", action="store_true", help="list the chains that match, without exporting anything")
    args = parser.parse_args()

    cfg = load_config(Path(args.config).expanduser())
    browser = None if args.dry_run else find_browser(cfg)       # checked before the long download
    cache = HERE / "cache"
    db = open_db(cache / "index.sqlite3")

    print("Step 1 of 3: getting the mail")
    if cfg["source"] == "folder":
        index_eml_folder(cfg, db)
    elif args.offline:
        print("   (offline: using what was downloaded previously)")
    else:
        sync_from_bridge(cfg, db, cache)

    print("Step 2 of 3: searching")
    messages = load_messages(cfg, db)
    chains = build_chains(messages, cfg["link_by_subject"])
    matched = find_matches(chains, cfg)
    total = sum(len(c["messages"]) for c in matched)
    print(f"   {len(messages)} emails in {len(chains)} chains searched: "
          f"{len(matched)} chains ({total} emails) match.")
    if not matched:
        print("\nNothing matched, so nothing was exported. Check the rules in config.yaml.")
        return

    # File names start with the date of the chain's first email, so they sort chronologically.
    out = unique_path(HERE / str(cfg["output_folder"]) / dt.datetime.now().strftime("%Y-%m-%d_%H%M"), set())
    names, jobs = set(), []
    for number, chain in enumerate(matched, 1):
        first = chain["messages"][0]
        # Seconds are included so chains that start in the same minute still sort in date order.
        stamp = show_time(first["date_ts"], cfg["tz"], "%Y-%m-%d_%H%M%S") if first["date_ts"] is not None else "0000-00-00_000000"
        who = slug(first["from_name"] or first["from_addr"].split("@")[0], 30)
        filename = f"{stamp}__{who}__{slug(base_subject(first['subject']) or 'no subject')}.pdf"
        jobs.append((chain, number, unique_path(out / "pdf" / filename, names)))

    if args.dry_run:
        for chain, number, path in jobs:
            print(f"   {number:>4}. {path.stem}   [{'; '.join(chain['reasons'])}]")
        print("\nDry run: nothing was exported.")
        return

    print(f"Step 3 of 3: writing {len(jobs)} PDFs with {Path(browser).name}")
    (out / "pdf").mkdir(parents=True)
    done = 0

    def work(job):
        nonlocal done
        entry = export_chain(*job, out, cfg, browser)
        done += 1
        print(f"   {done} of {len(jobs)}", end="\r", flush=True)
        return entry

    with ThreadPoolExecutor(max_workers=max(1, int(cfg["pdf"]["parallel"]))) as pool:
        entries = list(pool.map(work, jobs))
    print()
    write_index(entries, out, cfg)

    failed = sum(1 for e in entries if e.get("pdf_failed"))
    if failed:
        print(f"   Note: {failed} chain(s) couldn't be printed to PDF and were saved as HTML instead.")
    print(f"\nDone. Open this file to browse the results:\n   {out / 'index.html'}")
    if cfg["open_when_done"] and sys.platform == "darwin":
        subprocess.run(["open", str(out / "index.html")], check=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run it again to carry on; anything already downloaded is kept.")
        sys.exit(130)
