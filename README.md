# NU Direct Fetch — Honours 2025-26 (Termux / PC)

Hasan-এর দুইটা মূল API (`_nuresult.php` + `_nuAdmit.php`) যেই NU সার্ভার কল করে,
এই স্ক্রিপ্ট সেই একই কল **সরাসরি নিজের ফোন/PC থেকে** করে — মাঝখানে
`hasan.bro.bd` লাগে না। তাই Hostinger-এর PHP পুলের বাধা নেই, আর একসাথে
অনেক worker চালানো যায়।

আউটপুট `_nuCombined.php` v2-র JSON-এর মতোই একদম একই ফরম্যাট — তাই
সাইটের DB-তে আপলোড করতে কোনো বদল লাগবে না।

## ফাইল

| ফাইল | কাজ |
|---|---|
| `nu_direct_fetch.py` | মূল স্ক্রিপ্ট (শুধু Python 3, stdlib only) |
| `colleges-map.json` | ২,২৯৭ কলেজের নাম → code ম্যাপ (collegeCode বের করতে) |

## প্রথমে পরীক্ষা করুন (জরুরি)

NU-র সার্ভারে আপনার নেটওয়ার্ক থেকে সরাসরি যাওয়া যায় কিনা, সেটা আগে
একটা ID দিয়ে পরীক্ষা করুন। JSON দেখালে সরাসরি চলবে, ভুল দেখালে আপনার
নেটওয়ার্ক থেকে NU সরাসরি খোলে না — তখন আগের Hostinger-পথেই চালাতে হবে।

```sh
python3 nu_direct_fetch.py --test 3042425
```

## Termux-এ চালানো

```sh
pkg install python          # python না থাকলে; থাকলে লাগবে না
git clone https://github.com/mhasanm828/nu-direct-fetch.git
cd nu-direct-fetch
termux-wake-lock            # ফোন ঘুমালে Termux যাতে না মরে

python3 nu_direct_fetch.py --group science --workers 5
python3 nu_direct_fetch.py --group humanities --workers 5
python3 nu_direct_fetch.py --group business --workers 5
```

একেক গ্রুপের জন্য আলাদা Termux session/tab খুলে চালান। থামাতে `Ctrl+C`
(Volume Down + C)। ফোন চার্জে লাগিয়ে রাখাই ভালো।

## PC-তে চালানো

```sh
python3 nu_direct_fetch.py --group science --workers 10
```

## সেভ করার উপায় (`--save`)

| অপশন | মানে |
|---|---|
| `both` (ডিফল্ট) | ফোনে + সার্ভারে — দুই জায়গায় সেভ হবে |
| `local` | শুধু ফোনে — সার্ভারে কিছু পাঠাবে না |
| `server` | শুধু সার্ভারে — ফোনে ফাইল রাখবে না |

```sh
python3 nu_direct_fetch.py --group science --save local --workers 5
python3 nu_direct_fetch.py --group science --save server --workers 5
```

ফোনের ফাইল: `direct_science.jsonl` (complete), `direct_partial_science.jsonl`
(partial), আর progress ফাইল `direct_progress_science.json`।

## অন্যান্য অপশন

```sh
--start 3002005     # শুরুর ID (ডিফল্ট: science 3002005, humanities 5002001, business 7002001)
--limit 5000        # কয়টা ID স্ক্যান করবে
--workers 5         # একসাথে কয়টা request (১–২০, ডিফল্ট ৫)
--retries 3         # অসম্পূর্ণ রেকর্ডে retry
--fresh             # progress ফাইল উপেক্ষা করে --start থেকে শুরু
--dry-run           # কিছু fetch না করে শুধু পরিকল্পনাটা দেখাবে
```

আবার একই কমান্ড চালালে আগের জায়গা থেকেই শুরু হবে (progress ফাইল থেকে)।
২০০০টা পরপর খালি ID পেলে ব্লক শেষ ধরে নিজে থেমে যাবে।

## ফোনের ফাইল সাইটে আপলোড করতে (শুধু `--save local` ব্যবহার করলে)

`direct_<group>.jsonl` থেকে JSON array বানিয়ে Admin পেজে আপলোড করুন
(একবারে সর্বোচ্চ ৫,০০০ রেকর্ড):

```sh
python3 -c "import json; print(json.dumps([json.loads(l) for l in open('direct_science.jsonl')], ensure_ascii=False))" > upload_science.json
```

`--save both` (ডিফল্ট) ব্যবহার করলে এটা লাগবে না — সার্ভারের ফাইলে
সব জমা হয়, ঘণ্টার DB আপলোড আগের মতোই চলে।

## সতর্কতা

- workers বেশি মানেই সবসময় দ্রুত নয় — আসল সীমা NU-র সার্ভার (~২ সেকেন্ড/ID)।
  ৫ দিয়ে শুরু করুন; গতি + partial দেখে ১০ পর্যন্ত বাড়ান। partial/failed
  বাড়তে থাকলে workers কমান।
- মূল দুইটা API (`_nuresult.php`, `_nuAdmit.php`) এই স্ক্রিপ্ট ছোঁয় না —
  শুধু ওদের মতো একই NU কল করে।
