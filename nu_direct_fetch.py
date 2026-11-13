#!/usr/bin/env python3
"""
nu_direct_fetch.py — NU Honours 2025-26 DIRECT fetch (Termux / PC)
==================================================================
Hasan-এর দুইটা মূল API (_nuresult.php + _nuAdmit.php) যেই NU সার্ভার
কল করে, এই স্ক্রিপ্ট সেই একই কল সরাসরি নিজের ফোন/PC থেকে করে —
মাঝখানে hasan.bro.bd লাগে না। তাই Hostinger-এর PHP পুলের বাধা নেই,
আর একসাথে অনেক worker চালানো যায়।

আউটপুট _nuCombined.php v2-র JSON-এর মতোই একদম একই ফরম্যাট
(appid, name, roll, dob, father, mother, gender, ssc, hsc, choices,
status, allocated, group, collegeCode, ... ) — তাই সাইটের DB-তে
আপলোড করতে কোনো বদল লাগবে না।

সেভ করার উপায় (--save):
  both    ফোনে (local .jsonl) + সার্ভারে (save.php) — ডিফল্ট
  local   শুধু ফোনে — সার্ভারে কিছু পাঠাবে না
  server  শুধু সার্ভারে — ফোনে ফাইল রাখবে না

যা করে:
  - app55.nu.edu.bd-তে সরাসরি POST করে রেজাল্ট + অ্যাডমিট ডেটা আনে
  - complete রেকর্ড সেভ করে (ফোনে / সার্ভারে / দুই জায়গায়)
  - অসম্পূর্ণ রেকর্ড ৩ বার retry করে, তারপরও না হলে partial হিসেবে সেভ
  - খালি/Unknown ID আলাদা করে লগ করে
  - আগে সেভ করা ID আগেই বাদ দেয় (ফোনের ফাইল + সার্ভারের saved-ids)
  - Ctrl+C চাপলে বা ২০০০টা পরপর খালি ID পেলে থেমে progress সেভ করে;
    আবার একই কমান্ড চালালে সেখান থেকেই শুরু হয়।

শুধু Python 3 লাগে, আর কিছু না (stdlib only — pip install লাগে না)।

Termux-এ চালানো:
  pkg install python          (python না থাকলে; থাকলে লাগবে না)
  termux-wake-lock            (ফোন ঘুমালে Termux যাতে না মরে)
  python3 nu_direct_fetch.py --group science --workers 5
  python3 nu_direct_fetch.py --group humanities --workers 5
  python3 nu_direct_fetch.py --group business --workers 5
  (একেক গ্রুপের জন্য আলাদা Termux session/tab খুলে চালান।
   থামাতে Ctrl+C। ফোন চার্জে লাগিয়ে রাখাই ভালো।)

PC-তেও একইভাবে চলে:
  python3 nu_direct_fetch.py --group science --workers 10

একটা ID পরীক্ষা করতে (সেভ না করে):
  python3 nu_direct_fetch.py --test 3042425

শুধু ফোনে সেভ করতে:
  python3 nu_direct_fetch.py --group science --save local --workers 5

সতর্কতা: workers বেশি মানেই সবসময় দ্রুত নয় — আসল সীমা NU-র সার্ভার।
৫ দিয়ে শুরু করুন; গতি + partial দেখে ১০ পর্যন্ত বাড়ান।
partial/failed বাড়তে থাকলে workers কমান।
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

# ---- NU endpoints (Hasan-এর _nuresult.php / _nuAdmit.php-এর মতোই) ----
RESULT_URL = "http://app55.nu.edu.bd/nu-web/fetchAdmissionTestResultInformation"
ADMIT_URL = "http://app55.nu.edu.bd/nu-web/fetchAdmitCardInformation"
RESULT_REFERER = "http://app55.nu.edu.bd/nu-web/admissionTestResultQueryForm"
ADMIT_REFERER = "http://app55.nu.edu.bd/nu-web/admitCardVerificationHome"

# ---- সার্ভার সেভ (ব্রাউজার টুলের save.php-র মতোই) ----
BASE = "https://hasan.bro.bd"
SAVE = BASE + "/NU/bulk-honours/save.php"
COLLEGES_MAP_URL = BASE + "/_apis/colleges-map.json"

STOP_AFTER_INVALID = 2000   # পরপর এতগুলো খালি ID পেলে ব্লক শেষ ধরে থামবে
CHUNK = 100                 # একবারে এতগুলো ID submit করা হয়
SKIP_FLUSH = 50             # এতগুলো skipped জমলে সার্ভারে পাঠানো হয়
NU_TIMEOUT = 30             # NU-র একেকটা কলে সর্বোচ্চ সেকেন্ড

GROUP_STARTS = {"science": 3002005, "humanities": 5002001, "business": 7002001}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------
# HTML parsing — PHP দুইটার regex-এর মতোই
# ----------------------------------------------------------------------

def clean_text(text):
    """strip_tags + html_entity_decode + whitespace collapse + trim."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_field(html_text, field):
    """Result পেজ:  'Application ID :</font> VALUE </div>' ধরনের ফিল্ড।"""
    m = re.search(re.escape(field) + r"\s*:</font>\s*(.*?)</div>",
                  html_text, re.I | re.S)
    return clean_text(m.group(1)) if m else None


def parse_result(html_text):
    """_nuresult.php-এর মতো রেজাল্ট পার্স করে।"""
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
    """Admit পেজ:  '<strong>Label</strong>: <span>VALUE</span>' ধরনের ফিল্ড।"""
    m = re.search(r"<strong>\s*" + re.escape(label) + r"\s*</strong>:\s*<span[^>]*>(.*?)</span>",
                  html_text, re.I)
    return clean_text(m.group(1)) if m else ""


def table_value(table_html, label):
    """SSC/HSC টেবিল:  '<td>Label</td><td><span>VALUE</span>' ধরনের সেল।"""
    m = re.search(r"<td>\s*" + re.escape(label) + r"\s*</td>\s*<td>\s*<span[^>]*>(.*?)</span>",
                  table_html, re.I | re.S)
    return clean_text(m.group(1)) if m else ""


def parse_admit(html_text, roll):
    """_nuAdmit.php-এর মতো অ্যাডমিট পার্স করে।"""
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
# NU-তে সরাসরি POST
# ----------------------------------------------------------------------

def nu_post(url, referer, appid, timeout=NU_TIMEOUT):
    """একটা NU endpoint-এ form POST করে HTML ফেরত দেয়।"""
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
# কলেজ ম্যাপ (collegeCode বের করতে)
# ----------------------------------------------------------------------

def norm_name(s):
    s = (s or "").upper()
    s = s.replace("GOVERNMENT", "GOVT")
    for old in ("BROJOMOHUN", "BRAJOMOHUN", "BROJO MOHUN"):
        s = s.replace(old, "BM")
    return re.sub(r"[^A-Z0-9]", "", s)


_college_index = None


def college_code_for(college_name):
    """আবেদন করা কলেজের নাম থেকে code বের করে (colleges-map.json)।"""
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
    """দুইটা API-র ডেটা মিলিয়ে _nuCombined v2-র মতো একটাই রেকর্ড বানায়।"""
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
    """একটা ID-র জন্য দুইটা NU কল করে মার্জ করা রেকর্ড ফেরত দেয়।"""
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
# সার্ভার সেভ (save.php) + ফোনে সেভ (local .jsonl)
# ----------------------------------------------------------------------

def post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json",
                                         "User-Agent": "nu-direct/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def save_record_server(rec):
    """save.php-তে একটা রেকর্ড পাঠায়। 'saved' / 'duplicate' / None।"""
    for _ in range(2):  # ১ বার retry
        try:
            res = post_json(SAVE, rec)
            if isinstance(res, dict) and res.get("status"):
                return res.get("result") or "saved"
        except Exception:
            time.sleep(1)
    return None


def load_saved_ids_server():
    """v3 save.php থেকে আগে সেভ করা সব appid আনে। না পেলে খালি set।"""
    try:
        req = urllib.request.Request(SAVE + "?action=saved-ids",
                                    headers={"User-Agent": "nu-direct/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
        return {line.strip() for line in text.splitlines() if line.strip()}
    except Exception as e:
        log(f"সতর্কতা: সার্ভারের আগে-সেভ-করা ID-র লিস্ট আনা যায়নি ({e}) — ফিল্টার ছাড়াই চলছে।")
        return set()


def post_skipped_server(appids):
    """খালি ID-গুলো সার্ভারে লগ করে (v3 থাকলে)। ভুল হলে চুপচাপ বাদ।"""
    if not appids:
        return
    try:
        post_json(SAVE + "?action=skipped", {"appids": list(appids)})
    except Exception:
        pass


def load_local_ids(path):
    """ফোনের .jsonl ফাইল থেকে আগে সেভ করা appid-গুলো পড়ে।"""
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
    """ফোনের .jsonl ফাইলে একটা রেকর্ড যোগ করে।"""
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
            log(f"progress লেখা যায়নি: {e}")

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
        """রেকর্ডটা সেভ করে। returns: 'saved' / 'duplicate' / None (fail)।"""
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
                pass  # সার্ভারে ডুপ্লিকেট — অন্য জায়গায় সেভ হয়েছে কিনা দেখো
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
            self.backup_unsaved(rec)  # কোথাও সেভ হয়নি — লোকাল ব্যাকআপে রাখা হলো
            return ("failed", appid)
        if kind == "skip":
            return ("skip", appid)
        return ("fail", appid)

    def report(self):
        el = max(1, time.time() - self.t0)
        rate = self.processed / el * 60
        c = self.counts
        log(f"{self.processed}/{self.limit} | সেভ {c['saved']} | partial {c['partial']} | "
            f"dup {c['dup']} | খালি {c['skipped']} | ব্যর্থ {c['failed']} | "
            f"আগে-সেভ-বাদ {c['already']} | {rate:.1f}/মিনিট")

    def run(self):
        resumed = self.load_progress()
        if resumed:
            self.next_id = resumed
            log(f"আগের জায়গা থেকে চালাচ্ছি: {self.next_id} (progress ফাইল থেকে)")
        end = self.start + self.limit
        ids = list(range(self.next_id, end))
        if not ids:
            log("করার মতো ID নেই — range শেষ।")
            return

        already = set()
        if self.use_local:
            already |= load_local_ids(self.local_file)
            already |= load_local_ids(self.partial_file)
            if already:
                log(f"ফোনের ফাইলে আগে সেভ করা {len(already)}টা ID পাওয়া গেছে।")
        if self.use_server:
            server_ids = load_saved_ids_server()
            if server_ids:
                log(f"সার্ভারে আগে সেভ করা {len(server_ids)}টা ID পাওয়া গেছে।")
            already |= server_ids
        if already:
            before = len(ids)
            ids = [i for i in ids if str(i) not in already]
            self.counts["already"] = before - len(ids)
            log(f"আগে সেভ করা {self.counts['already']}টা ID বাদ দেওয়া হলো।")
        self.limit = len(ids)
        if not ids:
            log("সব ID-ই আগে সেভ করা — করার মতো কিছু নেই।")
            return
        log(f"গ্রুপ {self.group}: {len(ids)}টা ID স্ক্যান হবে "
            f"({ids[0]}–{ids[-1]}), workers={self.workers}, save={self.save_mode}")

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
                                log("২০০০টা পরপর খালি ID — ব্লক শেষ মনে হচ্ছে, থামানো হলো।")
                                self.stop = True
                                for f in futs:
                                    f.cancel()
                                break
                        else:
                            self.counts["failed"] += 1
                            self.consec_skipped = 0
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
                log("থামানো হচ্ছে (Ctrl+C) — progress সেভ করা হচ্ছে…")
                self.stop = True
        self.flush_skipped()
        self.write_progress(self.next_id)
        self.report()
        log(f"শেষ। পরের শুরু: {self.next_id} — আবার চালাতে একই কমান্ড দিন।")


def main():
    ap = argparse.ArgumentParser(description="NU Honours direct fetch (Termux/PC)")
    ap.add_argument("--group", choices=list(GROUP_STARTS), default="science",
                    help="science / humanities / business")
    ap.add_argument("--start", type=int, default=None,
                    help="Start ID (default: science 3002005, humanities 5002001, business 7002001)")
    ap.add_argument("--limit", type=int, default=5000,
                    help="কয়টা ID স্ক্যান করবে (default 5000)")
    ap.add_argument("--workers", type=int, default=5,
                    help="একসাথে কয়টা request (default 5; 1-20)")
    ap.add_argument("--delay", type=float, default=0,
                    help="প্রতি request-এর মাঝে সেকেন্ড (default 0)")
    ap.add_argument("--retries", type=int, default=3,
                    help="অসম্পূর্ণ রেকর্ডে retry (default 3)")
    ap.add_argument("--save", choices=["local", "server", "both"], default="both",
                    help="local=শুধু ফোনে, server=শুধু সার্ভারে, both=দুই জায়গায় (default both)")
    ap.add_argument("--fresh", action="store_true",
                    help="progress ফাইল উপেক্ষা করে --start থেকে শুরু করবে")
    ap.add_argument("--dry-run", action="store_true",
                    help="কিছু fetch করবে না — শুধু পরিকল্পনাটা দেখাবে")
    ap.add_argument("--test", type=int, default=None, metavar="APPID",
                    help="একটা ID fetch করে JSON দেখাবে, সেভ করবে না")
    args = ap.parse_args()
    if args.start is None:
        args.start = GROUP_STARTS[args.group]
    args.workers = max(1, min(20, args.workers))
    args.limit = max(1, args.limit)

    if args.test is not None:
        try:
            rec = fetch_one(args.test)
        except Exception as e:
            log(f"fetch ব্যর্থ: {e}")
            sys.exit(1)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        log(f"valid={is_valid(rec)} complete={is_complete(rec)}")
        return

    if args.dry_run:
        log(f"DRY RUN — গ্রুপ {args.group}: {args.limit}টা ID, "
            f"{args.start}–{args.start + args.limit - 1}, workers={args.workers}, save={args.save}")
        n_local = len(load_local_ids(f"direct_{args.group}.jsonl"))
        log(f"ফোনের ফাইলে আগে সেভ করা: {n_local}টা")
        if args.save in ("server", "both"):
            log(f"সার্ভারে আগে সেভ করা: {len(load_saved_ids_server())}টা")
        log("কিছু fetch করা হয়নি। চালাতে --dry-run বাদ দিন।")
        return

    Runner(args).run()


if __name__ == "__main__":
    main()
