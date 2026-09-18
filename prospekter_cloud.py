#!/usr/bin/env python3
"""
Automatisert prospektering - kjørt av en Claude Code sky-rutine (cron).

Henter bedrifter fra Brreg for et gitt postnummer, hopper over de som allerede
finnes i seen_orgnr.json (i dette repoet - committes tilbake etter hver kjøring),
henter kontaktperson (Brreg-roller), prøver å finne telefonnummer på proff.no og
1881.no, og oppretter kontaktperson + lead i NetHunt CRM via NetHunt sitt
legacy REST API (Basic Auth).

VIKTIG: Gjør IKKE næring/privat-sjekk via Kartserver.no - det krever personlig
innlogging og gjøres manuelt/interaktivt i etterkant.

Krever miljøvariabler: NETHUNT_EMAIL, NETHUNT_API_KEY
Bruk: python prospekter_cloud.py --postnummer 3510
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BRREG_API_BASE = "https://data.brreg.no/enhetsregisteret/api/enheter"
NETHUNT_BASE = "https://nethunt.com/api/v1/zapier"
FIBER_PIPE_FOLDER = "65afc684f6048909bb640cbb"
KUNDEKONTAKTER_FOLDER = "65afc684f6048909bb640cb9"
SEEN_FILE = Path(__file__).parent / "seen_orgnr.json"

ORG_FORMS = ["AS", "ENK", "ANS", "DA"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def nethunt_auth_header() -> dict:
    email = os.environ["NETHUNT_EMAIL"]
    key = os.environ["NETHUNT_API_KEY"]
    token = base64.b64encode(f"{email}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_companies(postnummer: str, org_forms: list[str] = ORG_FORMS) -> list[dict]:
    companies: list[dict] = []
    page = 0
    while True:
        params = {
            "organisasjonsform": ",".join(org_forms),
            "forretningsadresse.postnummer": postnummer,
            "size": 100,
            "page": page,
        }
        resp = requests.get(BRREG_API_BASE, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("_embedded", {}).get("enheter", [])
        companies.extend(batch)
        total_pages = data.get("page", {}).get("totalPages", 1)
        page += 1
        if page >= total_pages or not batch:
            break
    return companies


def is_relevant(company: dict) -> bool:
    if company.get("konkurs"):
        return False
    if company.get("underAvvikling"):
        return False
    if not company.get("forretningsadresse"):
        return False
    return True


def address_string(company: dict) -> str:
    addr = company.get("forretningsadresse", {}) or {}
    return ", ".join(
        filter(
            None,
            [
                " ".join(addr.get("adresse", []) or []),
                addr.get("postnummer"),
                addr.get("poststed"),
            ],
        )
    )


def fetch_contact_person(orgnr: str):
    resp = requests.get(f"{BRREG_API_BASE}/{orgnr}/roller", timeout=30)
    if resp.status_code != 200:
        return None
    candidates = []
    for gruppe in resp.json().get("rollegrupper", []):
        for rolle in gruppe.get("roller", []):
            if rolle.get("avregistrert"):
                continue
            navn = (rolle.get("person") or {}).get("navn") or {}
            if not navn.get("fornavn") or not navn.get("etternavn"):
                continue
            candidates.append(
                {
                    "fornavn": navn["fornavn"],
                    "etternavn": navn["etternavn"],
                    "stilling": rolle.get("type", {}).get("beskrivelse", ""),
                    "kode": rolle.get("type", {}).get("kode", ""),
                }
            )
    for kode in ("DAGL", "INNH", "LEDE"):
        for c in candidates:
            if c["kode"] == kode:
                return c
    return candidates[0] if candidates else None


def lookup_phone_proff(orgnr: str) -> str | None:
    """Beste-innsats: proff.no sitt bedriftssøk. Kan feile stille (SPA/rendering-endringer)."""
    try:
        resp = requests.get(
            "https://www.proff.no/bransjes%C3%B8k",
            params={"q": orgnr},
            headers=UA,
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        m = re.search(r"Telefon[^\d]{0,40}([\d][\d\s]{5,})", resp.text)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    except requests.RequestException:
        pass
    return None


def lookup_phone_1881(navn: str, poststed: str) -> str | None:
    """Beste-innsats: 1881.no sitt personsøk. Kan feile stille (SPA/rendering-endringer)."""
    try:
        resp = requests.get(
            "https://www.1881.no/persons",
            params={"query": navn, "where": poststed},
            headers=UA,
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        m = re.search(r'tel:([+\d][\d\s]{5,})', resp.text)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    except requests.RequestException:
        pass
    return None


def create_nethunt_contact(person: dict, company_name: str) -> str | None:
    fields = {
        "Name": f"{person['fornavn']} {person['etternavn']}",
        "Fornavn": person["fornavn"],
        "Etternavn": person["etternavn"],
        "Stilling": person["stilling"],
        "Navn på bedrift": company_name,
    }
    payload = {"timeZone": "Europe/Oslo", "fields": fields}
    resp = requests.post(
        f"{NETHUNT_BASE}/actions/create-record/{KUNDEKONTAKTER_FOLDER}",
        headers=nethunt_auth_header(),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  Feil ved oppretting av kontakt: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None
    return resp.json().get("recordId")


def create_nethunt_lead(company: dict, contact_id, person, phone) -> bool:
    addr = company.get("forretningsadresse", {}) or {}
    fields = {
        "Name": company.get("navn", ""),
        "Organisasjonsnummer": company.get("organisasjonsnummer", ""),
        "Adresse": address_string(company),
        "By": addr.get("poststed", ""),
        "Salgstrinn": "Nytt prospekt",
        "Kilde": "Egen Prosp",
    }
    if contact_id:
        fields["Navn på kontaktperson"] = [contact_id]
    if person:
        fields["Stilling"] = person["stilling"]
    if phone:
        fields["Telefonnummer"] = [phone]

    payload = {"timeZone": "Europe/Oslo", "fields": fields}
    resp = requests.post(
        f"{NETHUNT_BASE}/actions/create-record/{FIBER_PIPE_FOLDER}",
        headers=nethunt_auth_header(),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  Feil ved oppretting av lead: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postnummer", required=True)
    args = parser.parse_args()

    missing = [n for n in ("NETHUNT_EMAIL", "NETHUNT_API_KEY") if not os.environ.get(n)]
    if missing:
        print(f"Mangler miljøvariabler: {missing}", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    all_companies = fetch_companies(args.postnummer)
    relevant = [c for c in all_companies if is_relevant(c)]
    new_companies = [c for c in relevant if c.get("organisasjonsnummer") not in seen]

    added = 0
    failed = 0

    for company in new_companies:
        orgnr = company.get("organisasjonsnummer")
        navn = company.get("navn", "")
        print(f"Behandler {navn} ({orgnr})...", file=sys.stderr)

        person = fetch_contact_person(orgnr)
        contact_id = None
        phone = None

        if person:
            contact_id = create_nethunt_contact(person, navn)
            poststed = (company.get("forretningsadresse") or {}).get("poststed", "")
            phone = lookup_phone_1881(f"{person['fornavn']} {person['etternavn']}", poststed)

        if not phone:
            phone = lookup_phone_proff(orgnr)

        ok = create_nethunt_lead(company, contact_id, person, phone)
        if ok:
            added += 1
            seen.add(orgnr)
        else:
            failed += 1

        time.sleep(0.5)  # vær grei mot Brreg/proff/1881

    save_seen(seen)

    summary = {
        "postnummer": args.postnummer,
        "totalt_funnet": len(relevant),
        "allerede_kjent": len(relevant) - len(new_companies),
        "nye_lagt_til": added,
        "feilet": failed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
