"""Synthetic PHI corpus for measuring the flight scrubber's recall.

Every value is made up from the lists below with a seeded RNG; none is a
real person's. Each record has five fields (name, date of birth, record
number, phone, street address) and is written two ways:

* as text, from one of several templates (labelled form text, a sentence,
  tool-call arguments as JSON, a URL with the record number and name);
* as a rendered screenshot of a form (PIL, black on white).

    python corpus.py --seed 7 --count 50 --out corpus.json
"""

from __future__ import annotations

import argparse
import io
import json
import random
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

FIRST = [
    "Maria",
    "James",
    "Aaliyah",
    "Wei",
    "Olusegun",
    "Priya",
    "Dmitri",
    "Fatima",
    "Hiroshi",
    "Ingrid",
    "Kwame",
    "Lucia",
    "Mateo",
    "Nadia",
    "Oren",
    "Sione",
    "Tamsin",
    "Yusuf",
    "Zofia",
    "Ebony",
    "Rafael",
    "Mei",
    "Anders",
    "Chiamaka",
    "Declan",
    "Esperanza",
    "Farid",
    "Greta",
    "Haruto",
    "Imani",
]
LAST = [
    "Gonzalez",
    "Okafor",
    "Lindqvist",
    "Nakamura",
    "Petrov",
    "Abernathy",
    "Haddad",
    "Kowalczyk",
    "Mbeki",
    "Oyelaran",
    "Quintero",
    "Rasmussen",
    "Szabo",
    "Thibodeaux",
    "Vasquez",
    "Whitfield",
    "Xiong",
    "Yilmaz",
    "Zielinski",
    "Achterberg",
    "Bhattacharya",
    "Castellanos",
    "Dunleavy",
    "Ferreira",
    "Grünewald",
    "Hollingsworth",
    "Iwu",
    "Jablonski",
    "Kaur",
    "Loughran",
]
STREETS = [
    "Elm",
    "Maple",
    "Harbor View",
    "Old Mill",
    "Cedar Ridge",
    "Juniper",
    "Lakeshore",
    "Willow Bend",
    "Prospect",
    "Granite",
    "Sycamore",
    "Beacon",
]
SUFFIX = [
    "Street",
    "St",
    "Avenue",
    "Ave",
    "Road",
    "Rd",
    "Lane",
    "Drive",
    "Court",
    "Way",
]
CITIES = [
    ("Springfield", "IL"),
    ("Riverton", "WY"),
    ("Fairview", "OR"),
    ("Brookhaven", "GA"),
    ("Lakewood", "CO"),
    ("Milford", "CT"),
]
#: A long agent preamble (written for this corpus, naming no app). The
#: recall run puts it before each text, as a computer-use loop does.
LONG_PREAMBLE = (
    "You are a staff member using your organisation's web application in a "
    "web browser. The screen is 1280 by 800 pixels and you are already signed "
    "in. Use only this app, and change nothing the task doesn't ask for. Do "
    "the task and save it once. Take a screenshot before each click, and "
    "check the record on screen before you type anything.\n\n"
    "When you have finished, or if you can't finish, stop acting and end "
    "your reply with one final line in exactly one of these two forms:\n"
    "RESULT: DONE\n"
    "RESULT: NOT DONE <the reason, in one line>\n\n"
    "The task:\n"
)
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


@dataclass(frozen=True)
class Record:
    name: str
    dob: str
    mrn: str
    phone: str
    address: str
    city: str


def _record(rng: random.Random) -> Record:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    year, month, day = rng.randint(1930, 2015), rng.randint(1, 12), rng.randint(1, 28)
    dob = rng.choice(
        [
            f"{month:02d}/{day:02d}/{year}",
            f"{year}-{month:02d}-{day:02d}",
            f"{MONTHS[month - 1]} {day}, {year}",
            f"{day} {MONTHS[month - 1][:3]} {year}",
        ]
    )
    digits = "".join(str(rng.randint(0, 9)) for _ in range(rng.randint(6, 8)))
    mrn = rng.choice(
        [digits, f"A{digits}", f"{digits[:3]}-{digits[3:]}", f"MR{digits}"]
    )
    area, mid, end = rng.randint(201, 989), rng.randint(200, 999), rng.randint(0, 9999)
    phone = rng.choice(
        [
            f"({area}) {mid}-{end:04d}",
            f"{area}-{mid}-{end:04d}",
            f"{area}.{mid}.{end:04d}",
        ]
    )
    address = f"{rng.randint(1, 9899)} {rng.choice(STREETS)} {rng.choice(SUFFIX)}"
    city, state = rng.choice(CITIES)
    return Record(
        name=f"{first} {last}",
        dob=dob,
        mrn=mrn,
        phone=phone,
        address=address,
        city=f"{city}, {state} {rng.randint(10000, 99999)}",
    )


def _text(rng: random.Random, r: Record) -> str:
    templates = [
        lambda: (
            f"Patient: {r.name}\nDOB: {r.dob}\nMRN: {r.mrn}\n"
            f"Phone: {r.phone}\nAddress: {r.address}, {r.city}"
        ),
        lambda: (
            f"{r.name} (born {r.dob}) called from {r.phone} about chart {r.mrn}; "
            f"she still lives at {r.address} in {r.city}."
        ),
        lambda: json.dumps(
            {
                "patient": r.name,
                "birth_date": r.dob,
                "record": r.mrn,
                "contact": r.phone,
                "street": r.address,
            }
        ),
        lambda: (
            f"Opened https://emr.example.test/patients/{r.mrn}/chart?"
            f"name={quote(r.name)}&dob={quote(r.dob)} for {r.name}; callback "
            f"{r.phone}, {r.address}."
        ),
    ]
    return rng.choice(templates)()


def render_form(r: Record, size: tuple[int, int] = (900, 360)) -> bytes:
    """A form screenshot with the record's fields."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("Arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default(size=26)
    rows = [
        ("Patient name", r.name),
        ("Date of birth", r.dob),
        ("MRN", r.mrn),
        ("Phone", r.phone),
        ("Address", f"{r.address}, {r.city}"),
    ]
    for i, (label, value) in enumerate(rows):
        y = 24 + i * 64
        draw.text((24, y), label, fill=(60, 60, 60), font=font)
        draw.rectangle((250, y - 8, 880, y + 36), outline=(150, 150, 150))
        draw.text((262, y), value, fill=(0, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def build(seed: int, count: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for i in range(count):
        record = _record(rng)
        out.append({"index": i, "record": asdict(record), "text": _text(rng, record)})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(build(args.seed, args.count), handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
