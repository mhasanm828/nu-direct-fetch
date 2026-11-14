#!/usr/bin/env python3
"""
nu_direct_fetch.py — NU Honours 2025-26 DIRECT fetch (Termux / PC)
==================================================================
Version 2.0 FINAL — 14 Nov 2026.

This script calls the same NU server endpoints that Hasan's two main
APIs (_nuresult.php + _nuAdmit.php) call, directly from the phone/PC —
no hasan.bro.bd in the middle. So there is no Hostinger PHP-pool limit,
and many workers can run at once.

The output has exactly the same format as the _nuCombined.php v2 JSON
(appid, name, roll, dob, father, mother, gender, ssc, hsc, choices,
status, allocated, group, collegeCode, ... ) — so it uploads to the
site's database with no changes.

Save modes (--save):
  both    phone (local .jsonl) + server (save.php) — default
  local   phone only — sends nothing to the server
  server  server only — keeps no file on the phone

What it does:
  - POSTs directly to app55.nu.edu.bd for result + admit data
  - saves complete records (phone / server / both)
  - retries incomplete records 3 times, then saves them as partial
  - logs empty/Unknown IDs separately
  - skips already-saved IDs up front (phone files + server saved-ids)
  - Ctrl+C, or 2000 consecutive empty IDs, stops and saves progress;
    running the same command again resumes from there.

Only Python 3 is needed, nothing else (stdlib only — no pip install).

WHAT CHANGED IN v2.0 (14 Nov 2026):
  - All messages are now in English.
  - Live animated progress line (spinner): shows saved / partial /
    dup / empty / failed + speed, updating in place. Use --no-anim
    to turn the animation off (also auto-off when output is piped).
  - Startup status panel: every part shows [ENABLED] or [DISABLED],
    so you can see at a glance what is on or off — e.g. if the
    server's saved-ID list cannot be fetched, the skip filter shows
    [DISABLED] and already-saved IDs will be re-checked.
  - No change to fetching, saving, file names or record format —
    your local files (direct_*.jsonl, direct_progress_*.json)
    keep working exactly as before.

Run in Termux:
  pkg install python          (only if python is missing)
  termux-wake-lock            (keeps Termux alive when the phone sleeps)
  python3 nu_direct_fetch.py --group science --workers 5
  python3 nu_direct_fetch.py --group humanities --workers 5
  python3 nu_direct_fetch.py --group business --workers 5
  (Open a separate Termux session/tab for each group.
   Stop with Ctrl+C. Keep the phone on charge.)

Same on a PC:
  python3 nu_direct_fetch.py --group science --workers 10

Test one ID (no saving):
  python3 nu_direct_fetch.py --test 3042425

Save to phone only:
  python3 nu_direct_fetch.py --group science --save local --workers 5

Note: more workers is not always faster — the real limit is NU's
server. Start with 5; raise to 10 if speed + partial look good.
Lower the workers if partial/failed keeps growing.
"""

import argparse
import concurrent.futures
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- NU endpoints (same as _nuresult.php / _nuAdmit.php) ----
RESULT_URL = "http://app55.nu.edu.bd/nu-web/fetchAdmissionTestResultInformation"
ADMIT_URL = "http://app55.nu.edu.bd/nu-web/fetchAdmitCardInformation"
RESULT_REFERER = "http://app55.nu.edu.bd/nu-web/admissionTestResultQueryForm"
ADMIT_REFERER = "http://app55.nu.edu.bd/nu-web/admitCardVerificationHome"

# ---- Server save (same as the browser tool's save.php) ----
BASE = "https://hasan.bro.bd"
SAVE = BASE + "/NU/bulk-honours/save.php"
COLLEGES_MAP_URL = BASE + "/_apis/colleges-map.json"

STOP_AFTER_INVALID = 2000   # stop after this many consecutive empty IDs (end of block)
CHUNK = 100                 # IDs submitted per chunk
SKIP_FLUSH = 50             # skipped IDs buffered before sending to server
NU_TIMEOUT = 30             # max seconds per NU call

GROUP_STARTS = {"science": 3002005, "humanities": 5002001, "business": 7002001}

# ---- Animated progress line (spinner) ----
ANIM_ENABLED = True
_SPIN_FRAMES = "|/-\\"
_spin = {"i": 0, "len": 0}


def spin(text):
    """Rewrite one animated progress line in place (spinner + counts)."""
    if not ANIM_ENABLED:
        return
    frame = _SPIN_FRAMES[_spin["i"] % len(_SPIN_FRAMES)]
    _spin["i"] += 1
    line = "\r" + frame + " " + text
    pad = " " * max(0, _spin["len"] - len(line))
    sys.stdout.write(line + pad)
    sys.stdout.flush()
    _spin["len"] = len(line)


def clear_spin():
    """Erase the spinner line so a log line prints cleanly."""
    if _spin["len"]:
        sys.stdout.write("\r" + " " * _spin["len"] + "\r")
        sys.stdout.flush()
        _spin["len"] = 0


def log(msg):
    clear_spin()
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------
# HTML parsing — same regexes as the two PHP files
# ----------------------------------------------------------------------

def clean_text(text):
    """strip_tags + html_entity_decode + whitespace collapse + trim."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_field(html_text, field):
    """Result page: 'Application ID :</font> VALUE </div>' style fields."""
    m = re.search(re.escape(field) + r"\s*:</font>\s*(.*?)</div>",
                  html_text, re.I | re.S)
    return clean_text(m.group(1)) if m else None


def parse_result(html_text):
    """Parse the result page, like _nuresult.php."""
    app_id = get_field(html_text, "Application ID")
    roll = get_field(html_text, "Admission Test Roll No")
    name = get_field(html_text, "Applicant Name")
    full_result = get_field(html_text, "Result") or ""

    lower = full_result.lower()
    status = "Unknown"
    subject = None
    subject_code = None
    college_name = None
    college_code = None

    if "absent" in lower:
        status = "Absent"
    elif "failed" in lower or "not included" in lower:
        status = "Failed"
    elif "passing marks" in lower and "do not succeed" in lower:
        status = "Passed But No Merit"
    elif "assigned" in lower:
        status = "Passed And Got Merit"
        m = re.search(r"assigned\s+(.*?)\s+\((\d+)\)", full_result, re.I)
        if m:
            subject, subject_code = m.group(1).strip(), m.group(2).strip()
        m = re.search(r"in\s+(.*?)\s+\((\d+)\)", full_result, re.I)
        if m:
            college_name, college_code = m.group(1).strip(), m.group(2).strip()

    return {
        "application_id": app_id,
        "roll": roll,
        "applicant_name": name,
        "result_status": status,
        "full_result": full_result,
        "college_name": college_name,
        "college_code": college_code,
        "subject": subject,
        "subject_code": subject_code,
    }


def extract_value(html_text, label):
    """Admit page: '<strong>Label</strong>: <span>VALUE</span>' style fields."""
    m = re.search(r"<strong>\s*" + re.escape(label) + r"\s*</strong>:\s*<span[^>]*>(.*?)</span>",
                  html_text, re.I)
    return clean_text(m.group(1)) if m else ""


def table_value(table_html, label):
    """SSC/HSC table: '<td>Label</td><td><span>VALUE</span>' style cells."""
    m = re.search(r"<td>\s*" + re.escape(label) + r"\s*</td>\s*<td>\s*<span[^>]*>(.*?)</span>",
                  table_html, re.I | re.S)
    return clean_text(m.group(1)) if m else ""


def parse_admit(html_text, roll):
    """Parse the admit page, like _nuAdmit.php."""
    html_text = re.sub(r"\s+", " ", html_text or "")

    m = re.search(r"SSC/Equivalent Exam(.*?)</table>", html_text, re.I | re.S)
    ssc_html = m.group(1) if m else ""
    m = re.search(r"HSC/Equivalent Exam(.*?)</table>", html_text, re.I | re.S)
    hsc_html = m.group(1) if m else ""

    subjects = [s.strip() for s in
                re.findall(r"<b>\d+\.</b>\s*([^,<]+)", html_text, re.I)]

    return {
        "admission_test_roll": extract_value(html_text, "Admission Test Roll No"),
        "application_id": extract_value(html_text, "Application ID"),
        "exam_center_code": extract_value(html_text, "Exam Center Code"),
        "exam_center_name": extract_value(html_text, "Exam Center Name"),
        "exam_center_district": extract_value(html_text, "Exam Center District"),
        "applicant_name": extract_value(html_text, "Applicant's Name"),
        "father_name": extract_value(html_text, "Father's Name"),
        "mother_name": extract_value(html_text, "Mother's Name"),
        "date_of_birth": extract_value(html_text, "Date of Birth"),
        "gender": extract_value(html_text, "Sex"),
        "photo": "https://admission.bmcollege.site/proxy.php?id=" + str(roll),
        "ssc": {
            "roll": table_value(ssc_html, "Roll"),
            "board": table_value(ssc_html, "Board"),
            "passing_year": table_value(ssc_html, "Passing Year"),
        },
        "hsc": {
            "roll": table_value(hsc_html, "Roll"),
            "board": table_value(hsc_html, "Board"),
            "passing_year": table_value(hsc_html, "Passing Year"),
        },
        "college": extract_value(html_text, "College Applied for"),
        "subject_choice": subjects,
        "quota": extract_value(html_text, "Quota"),
    }


# ----------------------------------------------------------------------
# Direct POST to NU
# ----------------------------------------------------------------------

def nu_post(url, referer, appid, timeout=NU_TIMEOUT):
    """POST one form to an NU endpoint and return the HTML."""
    data = urllib.parse.urlencode({"admissionRoll": str(appid)}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "http://app55.nu.edu.bd",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ----------------------------------------------------------------------
# College map (to find collegeCode)
# ----------------------------------------------------------------------

def norm_name(s):
    s = (s or "").upper()
    s = s.replace("GOVERNMENT", "GOVT")
    for old in ("BROJOMOHUN", "BRAJOMOHUN", "BROJO MOHUN"):
        s = s.replace(old, "BM")
    return re.sub(r"[^A-Z0-9]", "", s)


_college_index = None


def college_code_for(college_name):
    """Find the code for the applied college's name (colleges-map.json)."""
    global _college_index
    if not college_name:
        return ""
    if _college_index is None:
        _college_index = {}
        raw = None
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "colleges-map.json")
        if os.path.exists(local):
            try:
                with open(local, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = None
        if raw is None:
            try:
                req = urllib.request.Request(
                    COLLEGES_MAP_URL, headers={"User-Agent": "nu-direct/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                raw = []
        for c in raw or []:
            key = norm_name(c.get("name", ""))
            if key and key not in _college_index:
                _college_index[key] = str(c.get("code", ""))
    return _college_index.get(norm_name(college_name), "")


def edu_str(e):
    """{'roll','board','passing_year'} -> 'ROLL [BOARD] YEAR'."""
    if not e or not isinstance(e, dict):
        return ""
    return ((e.get("roll") or "") + " [" + (e.get("board") or "") + "] "
            + (e.get("passing_year") or "")).strip()


def merge_record(admit, result, roll):
    """Merge the two APIs' data into one _nuCombined v2-style record."""
    admit = admit or {}
    result = result or {}
    appid = str(admit.get("application_id") or result.get("application_id") or roll)

    first = appid[:1]
    group = ("SCIENCE" if first == "3" else
             "HUMANITIES" if first == "5" else
             "BUSINESS ADMINISTRATION" if first == "7" else "")

    choices = ""
    subs = admit.get("subject_choice") or []
    if isinstance(subs, list) and subs:
        choices = " , ".join(f"{i + 1}. {s}" for i, s in enumerate(subs))

    ssc = admit.get("ssc") if isinstance(admit.get("ssc"), dict) else {}
    hsc = admit.get("hsc") if isinstance(admit.get("hsc"), dict) else {}

    return {
        "appid": appid,
        "name": admit.get("applicant_name") or result.get("applicant_name") or "",
        "roll": str(admit.get("admission_test_roll") or result.get("roll") or ""),
        "dob": admit.get("date_of_birth") or "",
        "father": admit.get("father_name") or "",
        "mother": admit.get("mother_name") or "",
        "gender": admit.get("gender") or "",
        "ssc": edu_str(ssc),
        "hsc": edu_str(hsc),
        "choices": choices,
        "status": result.get("result_status") or "",
        "allocated": result.get("subject") or "",
        "group": group,
        "received": "",
        "mobile": "",
        "session": "2025-26",
        "collegeCode": college_code_for(admit.get("college") or ""),
        "ssc_roll": str(ssc.get("roll") or ""),
        "ssc_board": ssc.get("board") or "",
        "ssc_year": str(ssc.get("passing_year") or ""),
        "hsc_roll": str(hsc.get("roll") or ""),
        "hsc_board": hsc.get("board") or "",
        "hsc_year": str(hsc.get("passing_year") or ""),
        "exam_center_code": str(admit.get("exam_center_code") or ""),
        "exam_center_name": admit.get("exam_center_name") or "",
        "exam_center_district": admit.get("exam_center_district") or "",
        "quota": admit.get("quota") or "",
        "photo": admit.get("photo") or "",
        "assigned_college_code": str(result.get("college_code") or ""),
        "assigned_college_name": result.get("college_name") or "",
        "subject_code": str(result.get("subject_code") or ""),
        "result_text": result.get("full_result") or "",
        "permanent_address": "",
        "ssc_gpa": "",
        "hsc_gpa": "",
    }


def is_valid(d):
    return bool(d and isinstance(d, dict) and d.get("appid")
                and d.get("name") and d.get("status")
                and d["status"] != "Unknown")


def is_complete(d):
    return bool(d.get("dob") or d.get("father") or d.get("mother") or d.get("ssc"))


def fetch_one(appid, timeout=NU_TIMEOUT):
    """Two NU calls for one ID; returns the merged record."""
    result_html = nu_post(RESULT_URL, RESULT_REFERER, appid, timeout)
    admit_html = nu_post(ADMIT_URL, ADMIT_REFERER, appid, timeout)
    return merge_record(parse_admit(admit_html, appid),
                        parse_result(result_html), appid)


def fetch_with_retry(appid, retries, retry_delay=1.0):
    last_err = None
    last_data = None
    for attempt in range(retries + 1):
        try:
            d = fetch_one(appid)
            if not is_valid(d):
                return ("skip", d)
            if is_complete(d):
                return ("ok", d)
            last_data = d
        except Exception as e:  # timeout / HTTP error / parse error
            last_err = e
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    if last_data is not None:
        return ("partial", last_data)
    return ("fail", last_err)


# ----------------------------------------------------------------------
# Server save (save.php) + phone save (local .jsonl)
# ----------------------------------------------------------------------

def post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json",
                                         "User-Agent": "nu-direct/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def save_record_server(rec):
    """Send one record to save.php. Returns 'saved' / 'duplicate' / None."""
    for _ in range(2):  # 1 retry
        try:
            res = post_json(SAVE, rec)
            if isinstance(res, dict) and res.get("status"):
                return res.get("result") or "saved"
        except Exception:
            time.sleep(1)
    return None


def load_saved_ids_server():
    """Fetch all already-saved appids from save.php v3.
    Returns (ids, ok): ok is False when the server could not be reached,
    in which case the caller must run WITHOUT the server skip-filter."""
    try:
        req = urllib.request.Request(SAVE + "?action=saved-ids",
                                    headers={"User-Agent": "nu-direct/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
        return ({line.strip() for line in text.splitlines() if line.strip()}, True)
    except Exception as e:
        log(f"WARNING: could not fetch the server's already-saved ID list ({e}) — running WITHOUT the server skip-filter.")
        return (set(), False)


def post_skipped_server(appids):
    """Log empty IDs on the server (v3 only). Silently ignored on error."""
    if not appids:
        return
    try:
        post_json(SAVE + "?action=skipped", {"appids": list(appids)})
    except Exception:
        pass


def load_local_ids(path):
    """Read already-saved appids from a phone .jsonl file."""
    ids = set()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        ids.add(str(json.loads(line).get("appid", "")))
                    except Exception:
                        pass
        except Exception:
            pass
    ids.discard("")
    return ids


def append_local(path, rec):
    """Append one record to a phone .jsonl file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Runner:
    def __init__(self, args):
        self.args = args
        self.group = args.group
        self.start = args.start
        self.limit = args.limit
        self.workers = args.workers
        self.delay = args.delay
        self.retries = args.retries
        self.save_mode = args.save  # local / server / both
        self.use_local = self.save_mode in ("local", "both")
        self.use_server = self.save_mode in ("server", "both")
        self.prog_file = f"direct_progress_{self.group}.json"
        self.local_file = f"direct_{self.group}.jsonl"
        self.partial_file = f"direct_partial_{self.group}.jsonl"
        self.backup_file = f"unsaved_{self.group}.jsonl"
        self.counts = {"saved": 0, "partial": 0, "dup": 0,
                       "skipped": 0, "failed": 0, "already": 0}
        self.consec_skipped = 0
        self.skipped_batch = []
        self.stop = False
        self.t0 = time.time()
        self.processed = 0
        self.next_id = self.start
        self._resumed = False
        self._last_spin = 0.0

    def load_progress(self):
        if self.args.fresh or not os.path.exists(self.prog_file):
            return None
        try:
            with open(self.prog_file, encoding="utf-8") as f:
                p = json.load(f)
            nxt = int(p.get("next_id", 0))
            if nxt and self.start <= nxt < self.start + self.limit:
                return nxt
        except Exception:
            pass
        return None

    def write_progress(self, next_id):
        try:
            with open(self.prog_file, "w", encoding="utf-8") as f:
                json.dump({"group": self.group, "next_id": next_id,
                           "save": self.save_mode,
                           "counts": self.counts,
                           "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except Exception as e:
            log(f"Could not write progress: {e}")

    def backup_unsaved(self, rec):
        try:
            append_local(self.backup_file, rec)
        except Exception:
            pass

    def flush_skipped(self):
        if self.skipped_batch:
            if self.use_server:
                post_skipped_server(self.skipped_batch)
            self.skipped_batch = []

    def store_record(self, rec, kind):
        """Save the record. Returns 'saved' / 'duplicate' / None (failed)."""
        saved_anywhere = False
        dup_everywhere = True
        if self.use_local:
            try:
                path = self.local_file if kind == "ok" else self.partial_file
                append_local(path, rec)
                saved_anywhere = True
                dup_everywhere = False
            except Exception:
                dup_everywhere = False
        if self.use_server:
            res = save_record_server(rec)
            if res == "saved":
                saved_anywhere = True
                dup_everywhere = False
            elif res == "duplicate":
                pass  # duplicate on server — check if saved elsewhere
            else:
                dup_everywhere = False
        if saved_anywhere:
            return "saved"
        if dup_everywhere and (self.use_local or self.use_server):
            return "duplicate"
        return None

    def process_one(self, appid):
        if self.delay:
            time.sleep(self.delay)
        kind, data = fetch_with_retry(appid, self.retries)
        if kind in ("ok", "partial"):
            rec = dict(data)
            res = self.store_record(rec, kind)
            if res == "saved":
                return ("saved" if kind == "ok" else "partial", appid)
            if res == "duplicate":
                return ("dup", appid)
            self.backup_unsaved(rec)  # saved nowhere — kept in the local backup
            return ("failed", appid)
        if kind == "skip":
            return ("skip", appid)
        return ("fail", appid)

    def live_line(self, current_id=None):
        """One-line live status for the spinner."""
        el = max(1, time.time() - self.t0)
        rate = self.processed / el * 60
        c = self.counts
        tail = f" | ID {current_id}" if current_id else ""
        return (f"{self.processed}/{self.limit} | saved {c['saved']} | "
                f"partial {c['partial']} | dup {c['dup']} | empty {c['skipped']} | "
                f"failed {c['failed']} | {rate:.0f}/min{tail}")

    def report(self):
        el = max(1, time.time() - self.t0)
        rate = self.processed / el * 60
        c = self.counts
        log(f"{self.processed}/{self.limit} | saved {c['saved']} | partial {c['partial']} | "
            f"dup {c['dup']} | empty {c['skipped']} | failed {c['failed']} | "
            f"pre-saved skipped {c['already']} | {rate:.1f}/min")

    def status_panel(self, total, already_n, server_ok):
        """Startup panel: every part shows [ENABLED] or [DISABLED]."""
        bar = "=" * 60
        if self.use_local:
            local_s = f"[ENABLED]  direct_{self.group}.jsonl"
        else:
            local_s = f"[DISABLED] (--save {self.save_mode})"
        if self.use_server:
            if server_ok:
                server_s = "[ENABLED]  save.php"
            else:
                server_s = "[DISABLED] server unreachable — already-saved IDs will be re-checked"
        else:
            server_s = f"[DISABLED] (--save {self.save_mode})"
        if already_n > 0:
            skip_s = f"[ACTIVE]   {already_n} already-saved IDs preloaded (will be skipped)"
        elif self.use_server and not server_ok:
            skip_s = "[DISABLED] server list unreachable — running WITHOUT the server filter"
        else:
            skip_s = "[DISABLED] no already-saved IDs found"
        if self._resumed:
            resume_s = f"[RESUMED]  from {self.next_id} (progress file)"
        else:
            resume_s = f"[FRESH]    start at {self.next_id}"
        log(bar)
        log(f"NU DIRECT FETCH — v2.0 FINAL | group {self.group.upper()}")
        log(f"Range      : {self.next_id} - {self.next_id + total - 1} ({total} IDs to scan)")
        log(f"Workers    : {self.workers} | Save mode: {self.save_mode}")
        log(f"Local save : {local_s}")
        log(f"Server save: {server_s}")
        log(f"Skip filter: {skip_s}")
        log(f"Resume     : {resume_s}")
        log(bar)

    def run(self):
        resumed = self.load_progress()
        if resumed:
            self.next_id = resumed
            self._resumed = True
            log(f"Resuming from {self.next_id} (progress file)")
        end = self.start + self.limit
        ids = list(range(self.next_id, end))
        if not ids:
            log("No IDs to do — range is finished.")
            return

        already = set()
        server_ok = True
        if self.use_local:
            already |= load_local_ids(self.local_file)
            already |= load_local_ids(self.partial_file)
            if already:
                log(f"Found {len(already)} already-saved IDs in the phone files.")
        if self.use_server:
            server_ids, server_ok = load_saved_ids_server()
            if server_ids:
                log(f"Found {len(server_ids)} already-saved IDs on the server.")
            already |= server_ids
        if already:
            before = len(ids)
            ids = [i for i in ids if str(i) not in already]
            self.counts["already"] = before - len(ids)
            log(f"Skipped {self.counts['already']} already-saved IDs.")
        self.limit = len(ids)
        if not ids:
            log("All IDs already saved — nothing to do.")
            return
        log(f"Group {self.group}: will scan {len(ids)} IDs "
            f"({ids[0]}-{ids[-1]}), workers={self.workers}, save={self.save_mode}")
        self.status_panel(len(ids), len(already), server_ok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as ex:
            try:
                for ci in range(0, len(ids), CHUNK):
                    if self.stop:
                        break
                    chunk = ids[ci:ci + CHUNK]
                    futs = {ex.submit(self.process_one, i): i for i in chunk}
                    unfinished = set(chunk)
                    for fut in concurrent.futures.as_completed(futs):
                        appid = futs[fut]
                        unfinished.discard(appid)
                        try:
                            kind, _ = fut.result()
                        except Exception:
                            kind = "fail"
                        self.processed += 1
                        if kind in ("saved", "partial"):
                            self.counts["saved" if kind == "saved" else "partial"] += 1
                            self.consec_skipped = 0
                        elif kind == "dup":
                            self.counts["dup"] += 1
                            self.consec_skipped = 0
                        elif kind == "skip":
                            self.counts["skipped"] += 1
                            self.skipped_batch.append(str(appid))
                            self.consec_skipped += 1
                            if len(self.skipped_batch) >= SKIP_FLUSH:
                                self.flush_skipped()
                            if self.consec_skipped >= STOP_AFTER_INVALID:
                                log("2000 consecutive empty IDs — end of block suspected, stopping.")
                                self.stop = True
                                for f in futs:
                                    f.cancel()
                                break
                        else:
                            self.counts["failed"] += 1
                            self.consec_skipped = 0
                        if time.time() - self._last_spin >= 0.2:
                            self._last_spin = time.time()
                            spin(self.live_line(appid))
                        if self.processed % 50 == 0:
                            self.report()
                    done_upto = chunk[-1] + 1
                    if unfinished:
                        done_upto = min(unfinished)
                    self.next_id = done_upto
                    self.write_progress(done_upto)
                    if self.stop:
                        break
            except KeyboardInterrupt:
                log("Stopping (Ctrl+C) — saving progress...")
                self.stop = True
        self.flush_skipped()
        self.write_progress(self.next_id)
        clear_spin()
        self.report()
        log(f"Done. Next start: {self.next_id} — run the same command again to continue.")


def main():
    ap = argparse.ArgumentParser(description="NU Honours direct fetch (Termux/PC) — v2.0 FINAL")
    ap.add_argument("--group", choices=list(GROUP_STARTS), default="science",
                    help="science / humanities / business")
    ap.add_argument("--start", type=int, default=None,
                    help="Start ID (default: science 3002005, humanities 5002001, business 7002001)")
    ap.add_argument("--limit", type=int, default=5000,
                    help="How many IDs to scan (default 5000)")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel requests (default 5; 1-20)")
    ap.add_argument("--delay", type=float, default=0,
                    help="Seconds between requests (default 0)")
    ap.add_argument("--retries", type=int, default=3,
                    help="Retries for incomplete records (default 3)")
    ap.add_argument("--save", choices=["local", "server", "both"], default="both",
                    help="local=phone only, server=server only, both=both places (default both)")
    ap.add_argument("--fresh", action="store_true",
                    help="Ignore the progress file and start from --start")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch nothing — just show the plan")
    ap.add_argument("--test", type=int, default=None, metavar="APPID",
                    help="Fetch one ID and show its JSON, without saving")
    ap.add_argument("--no-anim", action="store_true",
                    help="Turn off the animated progress line")
    args = ap.parse_args()
    if args.start is None:
        args.start = GROUP_STARTS[args.group]
    args.workers = max(1, min(20, args.workers))
    args.limit = max(1, args.limit)

    global ANIM_ENABLED
    ANIM_ENABLED = (not args.no_anim) and sys.stdout.isatty()

    if args.test is not None:
        try:
            rec = fetch_one(args.test)
        except Exception as e:
            log(f"Fetch failed: {e}")
            sys.exit(1)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        log(f"valid={is_valid(rec)} complete={is_complete(rec)}")
        return

    if args.dry_run:
        log(f"DRY RUN — group {args.group}: {args.limit} IDs, "
            f"{args.start}-{args.start + args.limit - 1}, workers={args.workers}, save={args.save}")
        n_local = len(load_local_ids(f"direct_{args.group}.jsonl"))
        log(f"Already saved on phone: {n_local} IDs")
        if args.save in ("server", "both"):
            sids, ok = load_saved_ids_server()
            log(f"Already saved on server: {len(sids)} IDs"
                + ("" if ok else " (list fetch FAILED — skip filter would be OFF)"))
        log("Nothing fetched. Run without --dry-run to start.")
        return

    Runner(args).run()


if __name__ == "__main__":
    main()
