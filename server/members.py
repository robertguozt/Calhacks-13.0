"""Demo members (PLAYBOOK.md §1). Dolores is CRS sandbox persona P20, bodies copied
exactly from CRS's personas.json: the sandbox only returns canned data when every
persona field is sent, otherwise the request goes to the real vendor."""

import json
import os
import pathlib

SEED_FILE = pathlib.Path(__file__).with_name("seed_ids.json")

_P20_ADDR = {"addressLine1": "612 OCEAN VIEW DR", "city": "SANTA BARBARA", "state": "CA", "postalCode": "93109"}
_P01_ADDR = {"addressLine1": "1420 WILLOW CREEK DR", "city": "SAN JOSE", "state": "CA", "postalCode": "95125"}

MEMBERS = {
    "dolores": {
        "name": "Dolores Whitcomb",
        "first_name": "Dolores",
        "age": 74,
        "phone_env": "MEMBER_PHONE",
        "family": {"name": "Susan", "relation": "daughter", "phone_env": "FAMILY_PHONE"},
        "shield_env": "SHIELD_NUMBER",  # her protected line (Scam Shield)
        "notes": "Retired school librarian, member since 1991. Lives alone. Grandson Leo's birthday is Oct 26.",
        "crs": {
            "identity": {
                "firstName": "DOLORES", "lastName": "WHITCOMB", "ssn": "666701020", "dob": "1952-05-04",
                "street1": "612 OCEAN VIEW DR", "city": "SANTA BARBARA", "state": "CA", "zip": "93109",
            },
            "fraud": {
                "firstName": "DOLORES", "lastName": "WHITCOMB", "phoneNumber": "5550101020",
                "email": "dolores.whitcomb@example.com", "ipAddress": "203.0.113.30", "address": _P20_ADDR,
            },
            "report": {
                "firstName": "DOLORES", "lastName": "WHITCOMB", "ssn": "666701020",
                "addresses": [{**_P20_ADDR, "borrowerResidencyType": "Current"}],
            },
        },
    },
    # Judge scenario: buys gift cards every year, so a $100 one should NOT be held.
    # He is CRS persona P01 (prime, clean file, identity PASS), renamed like Dolores is P20.
    "robert": {
        "name": "Robert Chen",
        "first_name": "Robert",
        "age": 71,
        "phone_env": "ROBERT_PHONE",
        "family": None,
        "notes": "Buys $50-100 gift cards for grandchildren every Christmas and birthday.",
        "crs": {
            "identity": {
                "firstName": "MARGARET", "lastName": "OKAFOR", "ssn": "666701001", "dob": "1984-03-14",
                "street1": "1420 WILLOW CREEK DR", "city": "SAN JOSE", "state": "CA", "zip": "95125",
            },
            "fraud": {
                "firstName": "MARGARET", "lastName": "OKAFOR", "phoneNumber": "5550101001",
                "email": "margaret.okafor@example.com", "ipAddress": "203.0.113.11", "address": _P01_ADDR,
            },
            "report": {
                "firstName": "MARGARET", "lastName": "OKAFOR", "ssn": "666701001",
                "addresses": [{**_P01_ADDR, "borrowerResidencyType": "Current"}],
            },
        },
    },
}


def phone(env: str | None) -> str | None:
    return os.getenv(env) if env else None


def member_phone(member_id: str) -> str | None:
    return phone(MEMBERS[member_id]["phone_env"]) or phone("MEMBER_PHONE")


def seed() -> dict:
    """{"members": {member_id: {"customer": id, "account": id}}, "merchants": {name: id}} from seed.py."""
    return json.loads(SEED_FILE.read_text()) if SEED_FILE.exists() else {"members": {}, "merchants": {}}


def by_phone(number: str | None) -> str | None:
    digits = lambda s: "".join(c for c in s or "" if c.isdigit())[-10:]
    for mid in MEMBERS:
        if number and digits(member_phone(mid)) == digits(number):
            return mid
    return None
