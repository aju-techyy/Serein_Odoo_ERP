"""Plans the demo world's history from real past forecasts.

Pure Python, no database: the loader gives it the target areas with their
hourly forecasts, the contracts, the fleet, and the crew, and gets back a
time-ordered list of events (alerts, missions, services, breakdowns). The
loader then plays the events through the module's own code, so every mission
still has to pass the readiness engine.

The rules it plans by are the ones the readiness engine checks: an aircraft
flies only from its home base, when airworthy and free; a crew member flies
only their own type, with a valid licence and medical and under the weekly
duty limit; a contract is flown at a steady pace through its season and never
past its budget. Every random choice is seeded by the alert it belongs to, so
the same weather always gives the same history.
"""
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .seeding_weather_snapshot import MAX_WINDOW_HOURS, seedability

# Alerts: the first seedable window of the local day, during daylight flying
# hours, inside the client's season.
DAYLIGHT_HOURS = (7, 15)          # local solar hours a window may start in
SEASON_MARGIN = timedelta(days=1)  # keep clear of the season's first and last day

# Missions.
TURNAROUND = timedelta(hours=1)      # between two flights of one aircraft or crew member
RESERVE_DAYS = 45                    # the final stretch in which a reserved aircraft goes first
SHORTEST_SORTIE = 1.0               # hours: a reserved aircraft's sortie is shortened, not below this
RECENT_USE = timedelta(days=10)      # crewed aircraft take turns by their flights in this period
CHECKS_BEFORE_TAKEOFF = timedelta(minutes=30)
SERVICE_DOWNTIME = timedelta(hours=30)  # a scheduled service, opened to closed
CREWED_HOURS = (1.6, 3.9)
DRONE_HOURS = (0.7, 1.4)
DRONE_SHARE = 0.3       # chance a free drone is sent instead of a crewed aircraft
MISSED_SHARE = 0.12     # alerts nobody acted on in time
CANCELLED_SHARE = 0.05  # missions planned, then cancelled before takeoff
RULES_SHARE = 0.35      # missions planned by the rule-based planner
PACE = 0.85             # share of the budget planned to be flown by the season's end
EARLY_ALLOWANCE = 6.0   # flight hours a contract may run ahead of its pace
BUDGET_CEILING = 0.97   # never plan past this share of the budget

CANCEL_REASONS = [
    "The cloud dissipated before takeoff.",
    "Cancelled on start-up: the flare rack failed its pre-flight test.",
    "The client asked to hold seeding over the area that day.",
]


@dataclass
class Area:
    key: str
    client: str
    base: str
    longitude: float
    times: list           # naive UTC datetimes, hourly
    scores: list          # seedability score per hour


@dataclass
class Contract:
    client: str
    season_start: object  # date
    season_end: object    # date
    budget: float


@dataclass
class Aircraft:
    key: str
    base: str
    kind: str             # 'crewed' or 'drone'
    rack: int
    interval: float


@dataclass
class Pilot:
    key: str
    base: str
    role: str             # 'pilot' or 'drone_operator'
    licence_expiry: object
    medical_expiry: object  # None for drone operators
    duty_limit: float


@dataclass
class Breakdown:
    aircraft: str
    opened_at: datetime
    closed_at: object     # datetime, or None while still open
    name: str


@dataclass
class Event:
    time: datetime
    kind: str             # 'alert', 'service_close', 'breakdown_open', 'breakdown_close'
    data: dict = field(default_factory=dict)

    ORDER = {'service_close': 0, 'breakdown_close': 1, 'breakdown_open': 2, 'alert': 3}

    def sort_key(self):
        return (self.time, self.ORDER[self.kind])


def hourly_scores(data):
    """(times, scores) of an hourly Open-Meteo response, scored as snapshots are."""
    hourly = data.get('hourly') or {}
    times = [datetime.strptime(t, '%Y-%m-%dT%H:%M') for t in hourly.get('time') or []]
    columns = [hourly.get(name) or [] for name in
               ('cloud_cover_low', 'cloud_cover_mid', 'cape', 'wind_speed_10m')]
    scores = []
    for index in range(len(times)):
        values = [(column[index] if index < len(column) else None) or 0.0 for column in columns]
        scores.append(seedability(*values)[3])
    return times, scores


def find_alerts(area, contract, threshold, until):
    """The alert windows of one area: per local day, the first hour in daylight
    whose score reaches the threshold, lasting while it stays there (at most
    MAX_WINDOW_HOURS). Only windows inside the season that end by `until`."""
    offset = timedelta(hours=round(area.longitude / 15.0))
    first_day = contract.season_start + SEASON_MARGIN
    last_day = contract.season_end - SEASON_MARGIN
    windows, days = [], set()
    count = len(area.times)
    for index, moment in enumerate(area.times):
        if area.scores[index] < threshold:
            continue
        local = moment + offset
        if not DAYLIGHT_HOURS[0] <= local.hour <= DAYLIGHT_HOURS[1] or local.date() in days:
            continue
        if not first_day <= moment.date() <= last_day:
            continue
        hours, cursor = 0, index
        while cursor < count and area.scores[cursor] >= threshold and hours < MAX_WINDOW_HOURS:
            hours += 1
            cursor += 1
        end = moment + timedelta(hours=hours)
        if end > until:
            continue
        days.add(local.date())
        windows.append((moment, end))
    return windows


@dataclass
class _PlaneState:
    hours: float = 0.0                 # since the last closed service
    busy_until: object = None
    service_until: object = None       # an open scheduled service closes then
    flights: list = field(default_factory=list)


@dataclass
class _PilotState:
    busy_until: object = None
    flights: list = field(default_factory=list)   # (landing, hours)


class HistoryPlanner:
    """Plans the history once; `plan()` returns the events in time order."""

    def __init__(self, areas, contracts, aircraft, pilots, breakdowns, threshold, until,
                 seed='serein', reserve=None):
        self.areas = areas
        self.contracts = contracts
        self.aircraft = aircraft
        self.pilots = pilots
        self.breakdowns = breakdowns
        self.threshold = threshold
        self.until = until
        self.seed = seed
        # (aircraft key, since, hours limit): from `since`, the aircraft goes
        # first when it is free, and flies only while it stays under the limit.
        self.reserve = reserve

    # Planning -----------------------------------------------------------------

    def plan(self):
        self.planes = {key: _PlaneState() for key in self.aircraft}
        self.crew = {key: _PilotState() for key in self.pilots}
        self.used = {key: 0.0 for key in self.contracts}
        events = []
        for item in self.breakdowns:
            events.append(Event(item.opened_at, 'breakdown_open', {'aircraft': item.aircraft, 'name': item.name}))
            if item.closed_at:
                events.append(Event(item.closed_at, 'breakdown_close', {'aircraft': item.aircraft, 'name': item.name}))
        windows = []
        for area in self.areas.values():
            for start, end in find_alerts(area, self.contracts[area.client], self.threshold, self.until):
                windows.append((start, area.key, end))
        for start, area_key, end in sorted(windows):
            events.extend(self._plan_alert(self.areas[area_key], start, end))
        return sorted(events, key=Event.sort_key)

    def _plan_alert(self, area, start, end):
        rng = random.Random('%s|%s|%s' % (self.seed, area.key, start.strftime('%Y%m%d%H')))
        # Every draw happens up front, in a fixed order, so a decision never
        # depends on how many draws an earlier branch made.
        missed = rng.random() < MISSED_SHARE
        delay = timedelta(minutes=rng.choice(range(10, 55, 5)))
        crewed_hours = round(rng.uniform(*CREWED_HOURS) * 12) / 12.0   # to 5 minutes
        drone_hours = round(rng.uniform(*DRONE_HOURS) * 12) / 12.0
        prefer_drone = rng.random() < DRONE_SHARE
        short = rng.choice([0, 0, 0, 1, 1, 2, 3])
        cancelled = rng.random() < CANCELLED_SHARE
        cancel_reason = rng.choice(CANCEL_REASONS)
        planned_by = 'rules' if rng.random() < RULES_SHARE else 'human'

        alert = Event(start, 'alert', {'area': area.key, 'start': start, 'end': end, 'outcome': 'expired'})
        contract = self.contracts[area.client]
        takeoff = start + delay
        if missed or takeoff >= end:
            return [alert]
        if not self._on_pace(contract, takeoff, crewed_hours):
            alert.data.update(outcome='dismissed', reason=[(None, 'on pace')])
            return [alert]

        plane, plane_reason = self._pick_aircraft(area.base, takeoff, crewed_hours, drone_hours, prefer_drone)
        if not plane:
            alert.data.update(outcome='dismissed', reason=plane_reason)
            return [alert]
        hours = self._sortie_hours(plane, takeoff, drone_hours if plane.kind == 'drone' else crewed_hours)
        landing = takeoff + timedelta(hours=hours)
        pilot, pilot_reason = self._pick_pilot(area.base, plane, takeoff, landing)
        if not pilot:
            alert.data.update(outcome='dismissed', reason=pilot_reason)
            return [alert]

        mission = {
            'aircraft': plane.key, 'pilot': pilot.key, 'takeoff': takeoff, 'landing': landing,
            'hours': hours, 'planned': plane.rack, 'short': short, 'planned_by': planned_by,
        }
        if cancelled:
            alert.data.update(outcome='cancelled', mission=mission, reason=cancel_reason)
            return [alert]
        alert.data.update(outcome='flown', mission=mission)
        alert.time = takeoff - CHECKS_BEFORE_TAKEOFF
        return [alert] + self._fly(contract, plane, pilot, takeoff, landing, hours)

    def _on_pace(self, contract, moment, hours):
        season = (contract.season_end - contract.season_start).days or 1
        elapsed = min(max((moment.date() - contract.season_start).days / season, 0.0), 1.0)
        planned = contract.budget * PACE * elapsed + EARLY_ALLOWANCE
        used = self.used[contract.client]
        return used + hours <= planned and used + hours <= contract.budget * BUDGET_CEILING

    def _pick_aircraft(self, base, takeoff, crewed_hours, drone_hours, prefer_drone):
        at_base = sorted((a for a in self.aircraft.values() if a.base == base),
                         key=lambda a: (-a.rack, a.key))
        reasons, free = [], []
        for plane in at_base:
            hours = drone_hours if plane.kind == 'drone' else crewed_hours
            why = self._plane_unavailable(plane, takeoff, takeoff + timedelta(hours=hours), hours)
            if why:
                reasons.append((plane.key, why))
            else:
                free.append(plane)
        if not free:
            return None, reasons
        drones = [p for p in free if p.kind == 'drone']
        # Crewed aircraft take turns: the least used lately flies, the larger
        # rack breaking a tie. A reserved aircraft in its final stretch goes
        # first, so it ends just under its limit.
        crewed = sorted((p for p in free if p.kind == 'crewed'),
                        key=lambda p: (not self._reserved(p, takeoff), self._recent_flights(p.key, takeoff),
                                       -p.rack, p.key))
        if drones and (prefer_drone or not crewed):
            return drones[0], None
        return crewed[0], None

    def _plane_unavailable(self, plane, takeoff, landing, hours):
        state = self.planes[plane.key]
        check = takeoff - CHECKS_BEFORE_TAKEOFF
        self._settle(plane.key, check)
        if state.service_until is not None:
            return 'service'
        for item in self.breakdowns:
            if item.aircraft != plane.key:
                continue
            # Margins of an hour keep the plan clear of the request's own timestamps.
            if item.opened_at <= landing + TURNAROUND and (
                    item.closed_at is None or item.closed_at >= check - TURNAROUND):
                return 'maintenance'
        if state.busy_until and state.busy_until + TURNAROUND > check:
            return 'busy'
        if state.hours >= plane.interval:
            return 'service'
        if self._reserved(plane, takeoff) and state.hours + SHORTEST_SORTIE > self.reserve[2]:
            return 'reserved'
        return None

    def _reserved(self, plane, takeoff):
        return bool(self.reserve) and plane.key == self.reserve[0] and takeoff >= self.reserve[1]

    def _sortie_hours(self, plane, takeoff, hours):
        """A reserved aircraft's sortie is shortened to stay under its limit."""
        if self._reserved(plane, takeoff):
            slack = self.reserve[2] - self.planes[plane.key].hours
            hours = min(hours, int(slack * 12) / 12.0)   # whole 5 minutes
        return hours

    def _recent_flights(self, key, moment):
        return sum(1 for takeoff, _landing, _hours in self.planes[key].flights
                   if moment - RECENT_USE < takeoff <= moment)

    def _settle(self, key, moment):
        state = self.planes[key]
        if state.service_until is not None and state.service_until <= moment:
            state.service_until = None
            state.hours = 0.0

    def _pick_pilot(self, base, plane, takeoff, landing):
        role = 'drone_operator' if plane.kind == 'drone' else 'pilot'
        check = takeoff - CHECKS_BEFORE_TAKEOFF
        day = check.date()
        candidates = []
        for pilot in sorted(self.pilots.values(), key=lambda p: p.key):
            if pilot.base != base or pilot.role != role:
                continue
            state = self.crew[pilot.key]
            # A day of margin: the engine judges dates in the user's time zone.
            if pilot.licence_expiry <= day + timedelta(days=1):
                continue
            if role == 'pilot' and (not pilot.medical_expiry or pilot.medical_expiry <= day + timedelta(days=1)):
                continue
            if state.busy_until and state.busy_until + TURNAROUND > check:
                continue
            duty = self.duty_hours(pilot.key, check)
            if duty >= pilot.duty_limit:
                continue
            candidates.append((duty, pilot.key, pilot))
        if not candidates:
            return None, [(plane.key, 'no crew')]
        return min(candidates)[2], None

    def duty_hours(self, pilot_key, moment):
        since = moment - timedelta(days=7)
        return sum(hours for landing, hours in self.crew[pilot_key].flights if since < landing <= moment)

    def _fly(self, contract, plane, pilot, takeoff, landing, hours):
        events = []
        state = self.planes[plane.key]
        state.busy_until = landing
        state.hours += hours
        state.flights.append((takeoff, landing, hours))
        crew = self.crew[pilot.key]
        crew.busy_until = landing
        crew.flights.append((landing, hours))
        self.used[contract.client] += hours
        if state.hours >= plane.interval:
            # Completing the mission opens the scheduled service; it is closed
            # after the service downtime.
            state.service_until = landing + SERVICE_DOWNTIME
            events.append(Event(landing + SERVICE_DOWNTIME, 'service_close', {'aircraft': plane.key}))
        return events

    # Results ------------------------------------------------------------------

    def hours_since_service(self, key):
        self._settle(key, self.until)
        return self.planes[key].hours


def plan_history(areas, contracts, aircraft, pilots, breakdowns, threshold, until,
                 reserved=None, reserve_limit=None):
    """Plan the history. With `reserved` (an aircraft key), the aircraft ends
    just under `reserve_limit` flight hours since its last service: over the
    last RESERVE_DAYS it goes first when free, flies only while it stays under
    the limit, and its last sortie is shortened to fit. Returns (events, planner)."""
    reserve = (reserved, until - timedelta(days=RESERVE_DAYS), reserve_limit) if reserved else None
    planner = HistoryPlanner(areas, contracts, aircraft, pilots, breakdowns, threshold, until, reserve=reserve)
    return planner.plan(), planner
