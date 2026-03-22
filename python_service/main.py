from __future__ import annotations

import html
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)'")


@dataclass(frozen=True)
class TamsQuery:
    movement_type: str
    airport_code: str
    sector: str
    airline_code: str
    landed_filter: str
    window_hours: int

    def to_form_data(self) -> dict[str, str]:
        return {
            "ddlMovTp": self.movement_type,
            "ddlAeropuerto": self.airport_code,
            "ddlSector": self.sector,
            "ddlAerolinea": self.airline_code,
            "ddlAterrizados": self.landed_filter,
            "ddlVentanaH": str(self.window_hours),
        }


@dataclass(frozen=True)
class FlightRecord:
    airline_code: str
    flight_number: str
    scheduled_raw: str
    scheduled_at_utc: str | None
    aircraft_registration: str
    position: str
    estimated_raw: str
    actual_raw: str
    terminal: str
    sector: str
    belt: str
    lf: str
    origin: str
    via: str
    remark: str
    sanitary: str
    passengers_raw: str
    passengers: int | None


@dataclass(frozen=True)
class ScrapeResult:
    captured_at_utc: str
    source_updated_at_utc: str | None
    query: TamsQuery
    total_flights: int
    total_pages: int
    source_url: str
    source_title: str
    notams_message: str | None
    flights: list[FlightRecord]


class LiveResponse(BaseModel):
    snapshot_id: int | None = None
    captured_at_utc: str
    source_updated_at_utc: str | None = None
    query: dict[str, Any]
    total_flights: int
    total_pages: int
    source_url: str
    source_title: str
    notams_message: str | None = None
    flights: list[dict[str, Any]]


class AppConfig(BaseModel):
    base_url: str = Field(default="http://www.tams.com.ar/organismos/vuelos.aspx")
    source_timezone: str = Field(default="America/Argentina/Buenos_Aires")
    sync_hours: int = Field(default=6)
    run_on_startup: bool = Field(default=True)
    db_path: str = Field(default="./data/tams_flights.db")
    default_query: dict[str, Any]

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            base_url=os.getenv("TAMS_BASE_URL", "http://www.tams.com.ar/organismos/vuelos.aspx"),
            source_timezone=os.getenv("TAMS_TIMEZONE", "America/Argentina/Buenos_Aires"),
            sync_hours=int(os.getenv("TAMS_SYNC_HOURS", "6")),
            run_on_startup=os.getenv("TAMS_RUN_ON_STARTUP", "true").lower() != "false",
            db_path=os.getenv("TAMS_DB_PATH", "./data/tams_flights.db"),
            default_query={
                "movement_type": os.getenv("TAMS_MOVEMENT_TYPE", "A").strip().upper(),
                "airport_code": os.getenv("TAMS_AIRPORT_CODE", "AEP").strip().upper(),
                "sector": os.getenv("TAMS_SECTOR", "-1").strip().upper(),
                "airline_code": os.getenv("TAMS_AIRLINE_CODE", "-1").strip().upper(),
                "landed_filter": os.getenv("TAMS_LANDED_FILTER", "TODOS").strip().upper(),
                "window_hours": int(os.getenv("TAMS_WINDOW_HOURS", "6")),
            },
        )

    def build_query(
        self,
        movement_type: str | None = None,
        airport_code: str | None = None,
        sector: str | None = None,
        airline_code: str | None = None,
        landed_filter: str | None = None,
        window_hours: int | None = None,
    ) -> TamsQuery:
        base = self.default_query
        return TamsQuery(
            movement_type=(movement_type or base["movement_type"]).strip().upper(),
            airport_code=(airport_code or base["airport_code"]).strip().upper(),
            sector=(sector or base["sector"]).strip().upper(),
            airline_code=(airline_code or base["airline_code"]).strip().upper(),
            landed_filter=(landed_filter or base["landed_filter"]).strip().upper(),
            window_hours=window_hours if window_hours is not None else int(base["window_hours"]),
        )


class SnapshotRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at_utc TEXT NOT NULL,
                    source_updated_at_utc TEXT,
                    movement_type TEXT NOT NULL,
                    airport_code TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    airline_code TEXT NOT NULL,
                    landed_filter TEXT NOT NULL,
                    window_hours INTEGER NOT NULL,
                    total_flights INTEGER NOT NULL,
                    total_pages INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    notams_message TEXT
                );

                CREATE TABLE IF NOT EXISTS flights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    row_number INTEGER NOT NULL,
                    airline_code TEXT NOT NULL,
                    flight_number TEXT NOT NULL,
                    scheduled_raw TEXT NOT NULL,
                    scheduled_at_utc TEXT,
                    aircraft_registration TEXT NOT NULL,
                    position TEXT NOT NULL,
                    estimated_raw TEXT NOT NULL,
                    actual_raw TEXT NOT NULL,
                    terminal TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    belt TEXT NOT NULL,
                    lf TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    via TEXT NOT NULL,
                    remark TEXT NOT NULL,
                    sanitary TEXT NOT NULL,
                    passengers_raw TEXT NOT NULL,
                    passengers INTEGER,
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );
                """
            )
            conn.commit()

    def insert_snapshot(self, result: ScrapeResult) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO snapshots (
                    captured_at_utc, source_updated_at_utc, movement_type, airport_code, sector,
                    airline_code, landed_filter, window_hours, total_flights, total_pages,
                    source_url, source_title, notams_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.captured_at_utc,
                    result.source_updated_at_utc,
                    result.query.movement_type,
                    result.query.airport_code,
                    result.query.sector,
                    result.query.airline_code,
                    result.query.landed_filter,
                    result.query.window_hours,
                    result.total_flights,
                    result.total_pages,
                    result.source_url,
                    result.source_title,
                    result.notams_message,
                ),
            )
            snapshot_id = int(cursor.lastrowid)

            for row_number, flight in enumerate(result.flights, start=1):
                conn.execute(
                    """
                    INSERT INTO flights (
                        snapshot_id, row_number, airline_code, flight_number, scheduled_raw,
                        scheduled_at_utc, aircraft_registration, position, estimated_raw, actual_raw,
                        terminal, sector, belt, lf, origin, via, remark, sanitary,
                        passengers_raw, passengers
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        row_number,
                        flight.airline_code,
                        flight.flight_number,
                        flight.scheduled_raw,
                        flight.scheduled_at_utc,
                        flight.aircraft_registration,
                        flight.position,
                        flight.estimated_raw,
                        flight.actual_raw,
                        flight.terminal,
                        flight.sector,
                        flight.belt,
                        flight.lf,
                        flight.origin,
                        flight.via,
                        flight.remark,
                        flight.sanitary,
                        flight.passengers_raw,
                        flight.passengers,
                    ),
                )

            conn.commit()
            return snapshot_id

    def get_latest(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            return self._snapshot_with_flights(conn, dict(row))

    def _snapshot_with_flights(self, conn: sqlite3.Connection, snapshot: dict[str, Any]) -> dict[str, Any]:
        flight_rows = conn.execute(
            "SELECT * FROM flights WHERE snapshot_id = ? ORDER BY row_number ASC",
            (snapshot["id"],),
        ).fetchall()
        return {
            "snapshot_id": snapshot["id"],
            "captured_at_utc": snapshot["captured_at_utc"],
            "source_updated_at_utc": snapshot["source_updated_at_utc"],
            "query": {
                "movement_type": snapshot["movement_type"],
                "airport_code": snapshot["airport_code"],
                "sector": snapshot["sector"],
                "airline_code": snapshot["airline_code"],
                "landed_filter": snapshot["landed_filter"],
                "window_hours": snapshot["window_hours"],
            },
            "total_flights": snapshot["total_flights"],
            "total_pages": snapshot["total_pages"],
            "source_url": snapshot["source_url"],
            "source_title": snapshot["source_title"],
            "notams_message": snapshot["notams_message"],
            "flights": [
                {
                    "row_number": flight["row_number"],
                    "airline_code": flight["airline_code"],
                    "flight_number": flight["flight_number"],
                    "scheduled_raw": flight["scheduled_raw"],
                    "scheduled_at_utc": flight["scheduled_at_utc"],
                    "aircraft_registration": flight["aircraft_registration"],
                    "position": flight["position"],
                    "estimated_raw": flight["estimated_raw"],
                    "actual_raw": flight["actual_raw"],
                    "terminal": flight["terminal"],
                    "sector": flight["sector"],
                    "belt": flight["belt"],
                    "lf": flight["lf"],
                    "origin": flight["origin"],
                    "via": flight["via"],
                    "remark": flight["remark"],
                    "sanitary": flight["sanitary"],
                    "passengers_raw": flight["passengers_raw"],
                    "passengers": flight["passengers"],
                }
                for flight in flight_rows
            ],
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


class TamsScraper:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.source_zone = self._resolve_source_zone(config.source_timezone)

    def scrape(self, query: TamsQuery) -> ScrapeResult:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            }
        )

        initial_html = self._get_page(session)
        initial_page = self._parse_page(initial_html)

        search_html = self._post_page(session, initial_page["hidden_fields"], query, event_target=None)
        current_page = self._parse_page(search_html)

        flights = list(current_page["flights"])
        visited_pages: set[int] = {current_page["current_page"]}
        seen_pages: set[int] = {current_page["current_page"]}
        pending_pages: list[tuple[int, str, dict[str, str]]] = []

        self._enqueue_page_links(current_page, pending_pages, seen_pages)

        while pending_pages:
            page_number, event_target, hidden_fields = pending_pages.pop(0)
            if page_number in visited_pages:
                continue

            page_html = self._post_page(session, hidden_fields, query, event_target=event_target)
            parsed_page = self._parse_page(page_html)
            resolved_page = parsed_page["current_page"] or page_number
            if resolved_page in visited_pages:
                continue

            visited_pages.add(resolved_page)
            flights.extend(parsed_page["flights"])
            self._enqueue_page_links(parsed_page, pending_pages, seen_pages)

        total_pages = max([current_page["current_page"], *current_page["page_links"].keys()], default=1)

        return ScrapeResult(
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
            source_updated_at_utc=current_page["source_updated_at_utc"],
            query=query,
            total_flights=len(flights),
            total_pages=total_pages,
            source_url=self.config.base_url,
            source_title=current_page["source_title"],
            notams_message=current_page["notams_message"],
            flights=flights,
        )

    def _get_page(self, session: requests.Session) -> str:
        response = session.get(self.config.base_url, timeout=60)
        response.raise_for_status()
        return response.text

    def _post_page(
        self,
        session: requests.Session,
        hidden_fields: dict[str, str],
        query: TamsQuery,
        event_target: str | None,
    ) -> str:
        form = dict(hidden_fields)
        form["__EVENTTARGET"] = event_target or ""
        form["__EVENTARGUMENT"] = ""
        form["__LASTFOCUS"] = ""
        form.update(query.to_form_data())
        if not event_target:
            form["btnBuscar"] = "Buscar"

        response = session.post(
            self.config.base_url,
            data=form,
            headers={"Referer": self.config.base_url},
            timeout=60,
        )
        response.raise_for_status()
        return response.text

    def _parse_page(self, page_html: str) -> dict[str, Any]:
        soup = BeautifulSoup(page_html, "html.parser")
        hidden_fields = {
            node.get("name", ""): node.get("value", "")
            for node in soup.select("form#form1 input[type='hidden']")
            if node.get("name")
        }

        source_title = soup.title.string.strip() if soup.title and soup.title.string else "AA2000 Organismos"
        source_updated = self._parse_source_updated_at(soup)
        page_links, current_page = self._parse_pager(soup)

        return {
            "hidden_fields": hidden_fields,
            "flights": self._parse_flights(soup, source_updated),
            "page_links": page_links,
            "current_page": current_page,
            "source_updated_at_utc": source_updated,
            "source_title": source_title,
            "notams_message": self._text(soup.select_one("#lnkbtnNotams")) or None,
        }

    def _parse_flights(self, soup: BeautifulSoup, source_updated_at_utc: str | None) -> list[FlightRecord]:
        flights: list[FlightRecord] = []
        for row in soup.select("table#dgGrillaA tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 16:
                continue

            first_cell = self._text(cells[0])
            second_cell = self._text(cells[1])
            if first_cell == "Cia." and second_cell == "Vuelo":
                continue

            scheduled_raw = self._text(cells[2])
            passengers_raw = self._text(cells[15])
            flights.append(
                FlightRecord(
                    airline_code=first_cell,
                    flight_number=second_cell,
                    scheduled_raw=scheduled_raw,
                    scheduled_at_utc=self._parse_scheduled_at(scheduled_raw, source_updated_at_utc),
                    aircraft_registration=self._text(cells[3]),
                    position=self._text(cells[4]),
                    estimated_raw=self._text(cells[5]),
                    actual_raw=self._text(cells[6]),
                    terminal=self._text(cells[7]),
                    sector=self._text(cells[8]),
                    belt=self._text(cells[9]),
                    lf=self._text(cells[10]),
                    origin=self._text(cells[11]),
                    via=self._text(cells[12]),
                    remark=self._text(cells[13]),
                    sanitary=self._text(cells[14]),
                    passengers_raw=passengers_raw,
                    passengers=int(passengers_raw) if passengers_raw.isdigit() else None,
                )
            )

        return flights

    def _parse_pager(self, soup: BeautifulSoup) -> tuple[dict[int, str], int]:
        pager_row = soup.select_one("table#dgGrillaA tr.Pager")
        if pager_row is None:
            pager_row = soup.select_one("table#dgGrillaA tr:has(a[href*='dgGrillaA$ctl24$'])")

        if pager_row is None:
            return {}, 1

        current_page = 1
        current_span = pager_row.find("span")
        if current_span and self._text(current_span).isdigit():
            current_page = int(self._text(current_span))

        page_links: dict[int, str] = {}
        for link in pager_row.find_all("a"):
            page_label = self._text(link)
            if not page_label.isdigit():
                continue

            href = html.unescape(link.get("href", ""))
            match = POSTBACK_RE.search(href)
            if match:
                page_links[int(page_label)] = match.group(1)

        return page_links, current_page

    def _parse_source_updated_at(self, soup: BeautifulSoup) -> str | None:
        raw_value = self._text(soup.select_one("#lblFechaActual"))
        if not raw_value:
            return None

        parsed = datetime.strptime(raw_value, "%d/%m/%Y %H:%M:%S")
        localized = parsed.replace(tzinfo=self.source_zone)
        return localized.astimezone(timezone.utc).isoformat()

    def _parse_scheduled_at(self, raw_value: str, source_updated_at_utc: str | None) -> str | None:
        if not raw_value or not source_updated_at_utc:
            return None

        try:
            parsed = datetime.strptime(raw_value, "%d/%m %H:%M")
        except ValueError:
            return None

        source_local = datetime.fromisoformat(source_updated_at_utc).astimezone(self.source_zone)
        candidate = datetime(
            year=source_local.year,
            month=parsed.month,
            day=parsed.day,
            hour=parsed.hour,
            minute=parsed.minute,
            tzinfo=self.source_zone,
        )

        if (candidate - source_local).days > 180:
            candidate = candidate.replace(year=candidate.year - 1)
        elif (source_local - candidate).days > 180:
            candidate = candidate.replace(year=candidate.year + 1)

        return candidate.astimezone(timezone.utc).isoformat()

    def _enqueue_page_links(
        self,
        page_data: dict[str, Any],
        pending_pages: list[tuple[int, str, dict[str, str]]],
        seen_pages: set[int],
    ) -> None:
        for page_number, event_target in page_data["page_links"].items():
            if page_number not in seen_pages:
                seen_pages.add(page_number)
                pending_pages.append((page_number, event_target, page_data["hidden_fields"]))

    @staticmethod
    def _text(node: Any) -> str:
        if node is None:
            return ""
        return html.unescape(node.get_text(" ", strip=True)).replace("\xa0", " ").strip()

    @staticmethod
    def _resolve_source_zone(zone_name: str) -> ZoneInfo:
        candidates = [zone_name]
        if zone_name == "America/Argentina/Buenos_Aires":
            candidates.append("America/Buenos_Aires")

        for candidate in candidates:
            try:
                return ZoneInfo(candidate)
            except ZoneInfoNotFoundError:
                continue

        return ZoneInfo("UTC")


class TamsService:
    def __init__(self, config: AppConfig, repository: SnapshotRepository, scraper: TamsScraper) -> None:
        self.config = config
        self.repository = repository
        self.scraper = scraper
        self._sync_lock = threading.Lock()

    def live(self, query: TamsQuery) -> dict[str, Any]:
        result = self.scraper.scrape(query)
        return self._to_live_response(None, result)

    def sync(self, query: TamsQuery | None = None) -> dict[str, Any]:
        with self._sync_lock:
            result = self.scraper.scrape(query or self.config.build_query())
            snapshot_id = self.repository.insert_snapshot(result)
            return self._to_live_response(snapshot_id, result)

    def latest(self) -> dict[str, Any] | None:
        return self.repository.get_latest()

    @staticmethod
    def _to_live_response(snapshot_id: int | None, result: ScrapeResult) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "captured_at_utc": result.captured_at_utc,
            "source_updated_at_utc": result.source_updated_at_utc,
            "query": asdict(result.query),
            "total_flights": result.total_flights,
            "total_pages": result.total_pages,
            "source_url": result.source_url,
            "source_title": result.source_title,
            "notams_message": result.notams_message,
            "flights": [
                {"row_number": index, **asdict(flight)}
                for index, flight in enumerate(result.flights, start=1)
            ],
        }


class SyncWorker:
    def __init__(self, service: TamsService, sync_hours: int, run_on_startup: bool) -> None:
        self.service = service
        self.sync_hours = sync_hours
        self.run_on_startup = run_on_startup
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None

    def _run(self) -> None:
        if self.run_on_startup:
            self._safe_sync()

        wait_seconds = max(self.sync_hours, 1) * 3600
        while not self.stop_event.wait(wait_seconds):
            self._safe_sync()

    def _safe_sync(self) -> None:
        try:
            self.service.sync()
        except Exception as exc:
            print(f"[sync-worker] error: {exc}")


config = AppConfig.load()
repository = SnapshotRepository(config.db_path)
repository.init_db()
scraper = TamsScraper(config)
service = TamsService(config, repository, scraper)
worker = SyncWorker(service, config.sync_hours, config.run_on_startup)


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="Python TAMS Microservice", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "timestamp_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/latest")
def latest() -> dict[str, Any]:
    snapshot = service.latest()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No hay snapshots guardados.")
    return snapshot


@app.get("/live")
def live(
    movement_type: str | None = None,
    airport_code: str | None = None,
    sector: str | None = None,
    airline_code: str | None = None,
    landed_filter: str | None = None,
    window_hours: int | None = Query(default=None, ge=-48, le=48),
) -> dict[str, Any]:
    query = config.build_query(
        movement_type=movement_type,
        airport_code=airport_code,
        sector=sector,
        airline_code=airline_code,
        landed_filter=landed_filter,
        window_hours=window_hours,
    )
    return service.live(query)


@app.post("/sync")
def sync() -> dict[str, Any]:
    return service.sync()
