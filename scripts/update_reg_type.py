#!/usr/bin/env python3
"""Refresh the Korean aircraft registration/type table used by the apps."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ATIS_URL = "http://atis.koca.go.kr/ATIS/aircraft/statList01.do"
ATIS_REFERER = (
    "http://atis.koca.go.kr/ATIS/aircraft/forwardPage.do?"
    "pageUrl=aircraftRegStat01"
)
ODCLOUD_URL = (
    "https://api.odcloud.kr/api/3048607/v1/"
    "uddi:ce7bfe62-e127-4066-8325-d814a360e3df"
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_CSV = ROOT / "reg_type.csv"
META_JSON = ROOT / "reg_type_meta.json"
UNMATCHED_CSV = ROOT / "unmatched_types.csv"
OVERRIDES_CSV = ROOT / "manual_overrides.csv"

COLUMNS = [
    "순번",
    "사업구분",
    "항공사",
    "등록기호",
    "형식",
    "제작번호",
    "제작일자",
    "기령(년)",
    "등록일자",
    "좌석",
    "최대이륙중량",
    "정치장",
    "도입형태",
    "ICAO",
    "IATA",
]

ATIS_FIELDS = {
    "순번": "SEQ",
    "사업구분": "PRJ_GBN",
    "항공사": "REG_CUSER",
    "등록기호": "REG_SNO",
    "형식": "AIR_TYPE",
    "제작번호": "AIR_BUILD_NO",
    "제작일자": "AIR_BUILD_DATE",
    "기령(년)": "AIR_AGE",
    "등록일자": "REG_DATE",
    "좌석": "AIR_LIMIT_MAN",
    "최대이륙중량": "AIR_FLY_WEIGHT",
    "정치장": "REG_JANG",
    "도입형태": "PROC_TYPE",
}


def request_json(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    # ATIS currently returns a JSON object encoded inside a JSON string.
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def fetch_atis() -> list[dict[str, object]]:
    url = f"{ATIS_URL}?AIR_GUBUN=all&_={int(time.time() * 1000)}"
    payload = request_json(
        url,
        {
            "User-Agent": "Mozilla/5.0 (compatible; aircraft-data-updater/1.0)",
            "Referer": ATIS_REFERER,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("ATIS response does not contain a data array")
    return rows


def fetch_odcloud(service_key: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page = 1
    # Accept either the encoded or decoded key shown by data.go.kr.
    decoded_key = urllib.parse.unquote(service_key.strip())
    while True:
        query = urllib.parse.urlencode(
            {"page": page, "perPage": 500, "serviceKey": decoded_key}
        )
        payload = request_json(
            f"{ODCLOUD_URL}?{query}", {"Accept": "application/json"}
        )
        batch = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(batch, list):
            raise RuntimeError("odcloud response does not contain a data array")
        rows.extend(batch)
        total = int(payload.get("totalCount", len(rows)))
        if not batch or len(rows) >= total:
            return rows
        page += 1


def normalized(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def first_value(row: dict[str, object], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def load_existing() -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, str]]]:
    by_registration: dict[str, dict[str, str]] = {}
    by_model: dict[str, tuple[str, str]] = {}
    if not OUTPUT_CSV.exists():
        return by_registration, by_model
    with OUTPUT_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            registration = normalized(row.get("등록기호"))
            model = normalized(row.get("형식"))
            icao = str(row.get("ICAO") or "").strip().upper()
            iata = str(row.get("IATA") or "").strip().upper()
            if registration:
                by_registration[registration] = row
            if model and icao:
                by_model[model] = (icao, iata)
    return by_registration, by_model


def build_public_lookup(rows: list[dict[str, object]]) -> dict[str, tuple[str, str]]:
    lookup: dict[str, tuple[str, str]] = {}
    for row in rows:
        model = normalized(
            first_value(row, ("비행기모델", "항공기모델", "MODEL", "Model"))
        )
        icao = first_value(
            row, ("항공기코드_ICAO", "ICAO코드", "ICAO CODE", "ICAO")
        ).upper()
        iata = first_value(
            row, ("항공기코드_IATA", "IATA코드", "IATA CODE", "IATA")
        ).upper()
        if model and icao:
            lookup[model] = (icao, iata)
    return lookup


def load_overrides() -> dict[str, tuple[str, str]]:
    overrides: dict[str, tuple[str, str]] = {}
    if not OVERRIDES_CSV.exists():
        return overrides
    with OVERRIDES_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            model = normalized(row.get("형식"))
            icao = str(row.get("ICAO") or "").strip().upper()
            iata = str(row.get("IATA") or "").strip().upper()
            if model and icao:
                overrides[model] = (icao, iata)
    return overrides


def build_rows(
    atis_rows: list[dict[str, object]],
    existing_by_registration: dict[str, dict[str, str]],
    existing_by_model: dict[str, tuple[str, str]],
    public_lookup: dict[str, tuple[str, str]],
    overrides: dict[str, tuple[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    output: list[dict[str, str]] = []
    unmatched: list[dict[str, str]] = []
    for source in atis_rows:
        row = {
            column: str(source.get(field) or "").strip()
            for column, field in ATIS_FIELDS.items()
        }
        registration = normalized(row["등록기호"])
        model = normalized(row["형식"])
        previous = existing_by_registration.get(registration, {})
        previous_codes = ("", "")
        # A registration can be reused after an aircraft change; only reuse it
        # when the model still matches.
        if normalized(previous.get("형식")) == model:
            previous_codes = (
                str(previous.get("ICAO") or "").strip().upper(),
                str(previous.get("IATA") or "").strip().upper(),
            )
        icao, iata = (
            overrides.get(model)
            or existing_by_model.get(model)
            or public_lookup.get(model)
            or previous_codes
        )
        row["ICAO"] = icao
        row["IATA"] = iata
        output.append({column: row.get(column, "") for column in COLUMNS})
        if not icao:
            unmatched.append({"등록기호": row["등록기호"], "형식": row["형식"]})
    return output, unmatched


def csv_bytes(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.seek(0)
        return b"\xef\xbb\xbf" + handle.read().encode("utf-8")


def validate(rows: list[dict[str, str]]) -> None:
    if len(rows) < 800:
        raise RuntimeError(f"refusing to publish only {len(rows)} ATIS rows")
    registrations = [normalized(row["등록기호"]) for row in rows]
    if any(not registration for registration in registrations):
        raise RuntimeError("ATIS contains an empty registration")
    if len(registrations) != len(set(registrations)):
        raise RuntimeError("ATIS contains duplicate registrations")


def main() -> int:
    service_key = os.environ.get("ODCLOUD_SERVICE_KEY", "").strip()
    existing_by_registration, existing_by_model = load_existing()
    atis_rows = fetch_atis()
    if service_key:
        public_rows = fetch_odcloud(service_key)
    else:
        public_rows = []
        print(
            "warning: ODCLOUD_SERVICE_KEY is not configured; "
            "using existing and manual type mappings",
            file=sys.stderr,
        )
    rows, unmatched = build_rows(
        atis_rows,
        existing_by_registration,
        existing_by_model,
        build_public_lookup(public_rows),
        load_overrides(),
    )
    validate(rows)

    content = csv_bytes(rows, COLUMNS)
    previous_content = OUTPUT_CSV.read_bytes() if OUTPUT_CSV.exists() else b""
    changed = content != previous_content
    if changed:
        OUTPUT_CSV.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        metadata = {
            "version": int(datetime.now(timezone.utc).timestamp()),
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "csvUrl": (
                "https://raw.githubusercontent.com/haanjhp/aircraft-data/"
                "main/reg_type.csv"
            ),
            "sha256": digest,
            "size": len(content),
            "rowCount": len(rows),
        }
        META_JSON.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if unmatched:
        UNMATCHED_CSV.write_bytes(csv_bytes(unmatched, ["등록기호", "형식"]))
    elif UNMATCHED_CSV.exists():
        UNMATCHED_CSV.unlink()

    print(
        f"ATIS={len(rows)}, odcloud={len(public_rows)}, "
        f"unmatched={len(unmatched)}, changed={str(changed).lower()}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"update failed: {error}", file=sys.stderr)
        sys.exit(1)
