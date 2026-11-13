import re

from odoo import _, api, fields, models

from .seeding_mission import RAINFALL_HOURS_AFTER_LANDING
from .seeding_readiness_check import READINESS_CHECKS
from .seeding_weather_snapshot import WIND_CALM, WIND_LIMIT

UTC_FORMAT = '%Y-%m-%d %H:%M'
MAX_SECTION_LENGTH = 1500   # characters per briefing section
MAX_SUMMARY_LENGTH = 3000   # characters
NUMBER_PATTERN = re.compile(r'\d+(?:,\d{3})*(?:\.\d+)?')

BRIEFING_SECTIONS = [
    ('overview', 'MISSION'),
    ('weather', 'WEATHER'),
    ('seeding_run', 'SEEDING RUN'),
    ('aircraft_and_load', 'AIRCRAFT AND LOAD'),
    ('hazards', 'HAZARDS'),
    ('abort_criteria', 'ABORT CRITERIA'),
]
BRIEFING_LISTS = {'hazards', 'abort_criteria'}

BRIEFING_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'overview': {'type': 'STRING', 'description': "the mission, area, client and crew in two or three sentences"},
        'weather': {'type': 'STRING', 'description': "the forecast readings and what they mean for seeding"},
        'seeding_run': {'type': 'STRING', 'description': "takeoff time, launch-by time and the seedable window"},
        'aircraft_and_load': {'type': 'STRING', 'description': "aircraft, base and flare load"},
        'hazards': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'abort_criteria': {
            'type': 'ARRAY', 'items': {'type': 'STRING'},
            'description': "changes that must stop the takeoff or end the run; never a hazard repeated",
        },
    },
    'required': [key for key, _label in BRIEFING_SECTIONS],
    'propertyOrdering': [key for key, _label in BRIEFING_SECTIONS],
}

# The summary comes back as three paragraphs, joined with blank lines.
SUMMARY_PARAGRAPHS = ['flight', 'conditions', 'results']
SUMMARY_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'flight': {'type': 'STRING', 'description': "paragraph 1: client, area, date, aircraft, crew, times, flight time"},
        'conditions': {'type': 'STRING', 'description': "paragraph 2: the cloud conditions that led to the flight"},
        'results': {'type': 'STRING', 'description': "paragraph 3: flares fired, material released, rainfall status"},
    },
    'required': SUMMARY_PARAGRAPHS,
    'propertyOrdering': SUMMARY_PARAGRAPHS,
}

# What the seedability score counts, given to the model with the readings.
SCORE_RULE = (
    "The seedability score counts the larger of low and mid cloud, CAPE, and calm wind. "
    "High cloud does not count toward it."
)

FIGURES_RULE = """Use only facts and figures from the input. Every number you write must
appear in the input, exactly or rounded (1.52 may be written 1.5). Do not calculate
new figures and do not invent limits, altitudes, headings, distances or frequencies.
Write times as HH:MM UTC. Write whole numbers without a decimal (96 %, not 96.0 %).
The seedability score counts the larger of low and mid cloud, CAPE, and calm wind;
high cloud does not count toward it, so never give high cloud as a reason for the score."""

BRIEFING_INSTRUCTIONS = """You write the pre-flight briefing for the crew of a cloud seeding mission.
The input holds the approved mission plan, the target area, the forecast readings
behind the cloud alert, the aircraft, the crew and the flare load.

%s

Sections:
- overview: the mission reference, target area, client and crew, two or three sentences.
- weather: the readings and what they mean for seeding: seedable cloud layers,
  instability, wind and gusts.
- seeding_run: planned takeoff, launch-by time and the seedable window.
- aircraft_and_load: aircraft, tail number, base, rack capacity and flare load.
- hazards: one short line per hazard, taken from the readings and planner_risks.
  Describe them as they are: wind and gusts up to wind_light_up_to_kmh are light, not "high".
  Leave out a planner risk the readings do not support. A favourable reading is not a hazard.
- abort_criteria: one short line per change that must stop the takeoff or end the
  seeding run, for example: the seedable cloud dissipates or moves out of the target
  area, conditions become worse than forecast, or the aircraft, crew or flare load
  differs from the plan. An abort criterion is a change from the plan, never the
  current forecast: do not repeat a hazard or a reading as an abort criterion. The
  launch-by limit is added from the record: leave it out.
Short operational sentences, no greetings, no markdown.
""" % FIGURES_RULE

SUMMARY_INSTRUCTIONS = """You write the client summary of a completed cloud seeding mission.
The client paid for the flight and reads this summary on the mission report.

%s

Rules:
- Three short paragraphs, 120 to 220 words in total, plain English, past tense, factual:
  flight: the client, target area, date, aircraft, crew, takeoff and landing, flight time.
  conditions: the cloud conditions that led to the flight, from conditions_at_alert.
  If conditions_at_alert is null, the flight was planned without a cloud alert: say so
  in one sentence and say whether the readiness checks passed before launch, without
  counting them. Do not describe weather the input does not give.
  results: the number of flares fired, the seeding material released, and rainfall.
  You may name the flare batches (flares.batches) the flares came from.
- If observed_rainfall_mm is null, say that observed rainfall has not been recorded yet.
  Otherwise give it with its period (observed_rainfall_period: from the takeoff hour to
  hours_after_landing hours after landing) and its source.
  Never claim or imply that the seeding caused or increased rainfall.
- Do not mention internal ids, costs, prices or how the text was written.
- Plain text only: no headings, no markdown, no bullet points.
""" % FIGURES_RULE


class TextRejected(Exception):
    """The model's text cannot be used."""


# A line that restates the launch-by limit itself ("the launch-by time passes").
LAUNCH_BY_PASSES = re.compile(
    r"launch[- ]by(?: time| limit)?\s+(?:has\s+|is\s+)?(?:pass|passes|passed|reached|exceeded|expires|expired)",
    re.IGNORECASE,
)


def _tidy(value):
    """Whole floats as integers, so the model writes 98 %, not 98.0 %."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _tidy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_tidy(v) for v in value]
    return value


def _utc(moment):
    return moment.strftime(UTC_FORMAT) if moment else None


def _num(value):
    """A figure as people write it: 12, 1.5, 1.52."""
    if value is None:
        return ''
    value = round(float(value), 2)
    return str(int(value)) if value.is_integer() else ('%.2f' % value).rstrip('0')


def _numbers(text):
    """Every number written in `text`, as (value, decimals)."""
    found = []
    for token in NUMBER_PATTERN.findall(text or ''):
        plain = token.replace(',', '')
        decimals = len(plain.split('.')[1]) if '.' in plain else 0
        found.append((float(plain), decimals, token))
    return found


def _plain(text):
    """Lower-case words only, for comparing two lines of text."""
    return ' '.join(re.findall(r'[a-z0-9.]+', (text or '').lower()))


def _allowed_numbers(payload):
    """Every number in the input, from values and from text such as dates
    and aircraft names."""
    values = set()

    def walk(node):
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            values.add(abs(float(node)))
        elif isinstance(node, str):
            values.update(value for value, _d, _t in _numbers(node))
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(key)
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return values


def unknown_figures(text, payload):
    """Numbers in `text` that are not in `payload`, exactly or rounded to the
    precision they are written with."""
    allowed = _allowed_numbers(payload)
    unknown = []
    for value, decimals, token in _numbers(text):
        tolerance = 0.5 * 10 ** -decimals + 1e-9
        if not any(abs(candidate - value) <= tolerance for candidate in allowed):
            unknown.append(token)
    return unknown


class SeedingMissionWriter(models.AbstractModel):
    """Writes the crew briefing (when a mission becomes ready) and the client
    summary (when it is completed). The language model drafts the text; the
    text is accepted only if every figure in it comes from the mission
    record. Otherwise, or when the model is unavailable, the text is written
    from a fixed template filled from the record."""
    _name = 'seeding.mission.writer'
    _description = 'Serein Mission Writer'

    # Inputs ---------------------------------------------------------------

    @api.model
    def _conditions(self, alert):
        if not alert:
            return None
        snap = alert.snapshot_id
        return {
            'alert': alert.name,
            'readings_for_utc': _utc(snap.valid_for),
            'seedability_score': alert.score,
            'score_out_of': 100,
            'score_breakdown': alert.score_breakdown,
            'score_counts': SCORE_RULE,
            'wind_light_up_to_kmh': WIND_CALM,
            'wind_scores_nothing_from_kmh': WIND_LIMIT,
            'window_start_utc': _utc(alert.window_start),
            'window_end_utc': _utc(alert.window_end),
            **snap._ai_readings(),
        }

    @api.model
    def _area(self, mission):
        area = mission.area_id
        return {
            'name': area.name,
            'country': area.country_id.name or None,
            'latitude': area.latitude,
            'longitude': area.longitude,
            'radius_km': area.radius_km,
        }

    @api.model
    def _aircraft(self, mission):
        plane = mission.aircraft_id
        return {
            'name': plane.name,
            'type': plane.aircraft_type or 'crewed',
            'tail_number': plane.tail_number or None,
            'rack_capacity': plane.rack_capacity,
            'base': mission.base_id.name,
        }

    @api.model
    def _crew(self, mission):
        return {
            'name': mission.pilot_id.name,
            'role': mission.pilot_id.pilot_role or 'pilot',
        }

    @api.model
    def _readiness(self, mission):
        results = mission.readiness_ids.mapped('result')
        return {
            'checks_total': len(READINESS_CHECKS),
            'checks_passed': results.count('pass'),
            'checks_not_applicable': results.count('na'),
            'checks_failed': results.count('fail'),
            'checked_at_utc': _utc(mission.readiness_checked_at),
        }

    @api.model
    def _briefing_input(self, mission):
        mission = mission.sudo()
        grams = mission.flare_product_id.grams_per_flare
        return _tidy({
            'mission': mission.name,
            'client': mission.partner_id.name or None,
            'target_area': self._area(mission),
            'planned_takeoff_utc': _utc(mission.planned_takeoff),
            'launch_by_utc': _utc(mission.launch_by),
            'conditions_at_alert': self._conditions(mission.alert_id),
            'aircraft': self._aircraft(mission),
            'crew': self._crew(mission),
            'load': {
                'flare_product': mission.flare_product_id.name or None,
                'flares_planned': mission.flares_planned,
                'grams_per_flare': grams,
                'material_planned_g': mission.flares_planned * grams,
                'material_planned_kg': round(mission.flares_planned * grams / 1000.0, 2),
            },
            'planner_risks': [
                line.lstrip('• ').strip() for line in (mission.plan_risks or '').splitlines() if line.strip()
            ],
            'readiness': self._readiness(mission),
        })

    @api.model
    def _summary_input(self, mission):
        mission = mission.sudo()
        minutes = int(round(mission.flight_hours * 60))
        return _tidy({
            'mission': mission.name,
            'client': mission.partner_id.name or None,
            'contract': mission.contract_id.name or None,
            'target_area': self._area(mission),
            'flight': {
                'date_utc': mission.takeoff_at.strftime('%Y-%m-%d') if mission.takeoff_at else None,
                'takeoff_utc': _utc(mission.takeoff_at),
                'landing_utc': _utc(mission.landing_at),
                'flight_time_hours': mission.flight_hours,
                'flight_time_minutes': minutes,
            },
            'aircraft': self._aircraft(mission),
            'crew': self._crew(mission),
            'flares': {
                'product': mission.flare_product_id.name or None,
                'loaded': mission.flares_planned,
                'fired': mission.flares_fired,
                'grams_per_flare': mission.flare_product_id.grams_per_flare,
                'material_released_g': mission.material_released_g,
                'material_released_kg': round(mission.material_released_g / 1000.0, 2),
                'batches': mission._flare_batch_lines(),
            },
            'conditions_at_alert': self._conditions(mission.alert_id),
            'readiness_before_launch': self._readiness(mission),
            'observed_rainfall_mm': (
                mission.observed_rainfall_mm if mission.rainfall_recorded_at else None
            ),
            'observed_rainfall_period': {
                'from_utc': _utc(mission.rainfall_period_start),
                'to_utc': _utc(mission.rainfall_period_end),
                'hours_after_landing': RAINFALL_HOURS_AFTER_LANDING,
                'source': mission.rainfall_source,
            } if mission.rainfall_recorded_at else None,
        })

    # Validation -----------------------------------------------------------

    @api.model
    def _check_figures(self, text, payload):
        unknown = unknown_figures(text, payload)
        if unknown:
            raise TextRejected(_(
                "The text states figures that are not in the mission record: %s.",
                ', '.join(dict.fromkeys(unknown)),
            ))

    @api.model
    def _validate_briefing(self, data, payload):
        sections = {}
        for key, _label in BRIEFING_SECTIONS:
            if key not in data:
                raise TextRejected(_("The briefing is missing the section %s.", key))
            value = data[key]
            if key in BRIEFING_LISTS:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise TextRejected(_("The briefing section %s must be a list of text.", key))
                value = [v.strip() for v in value if v.strip()]
                length = sum(len(v) for v in value)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise TextRejected(_("The briefing section %s is empty.", key))
                value = value.strip()
                length = len(value)
            if length > MAX_SECTION_LENGTH:
                raise TextRejected(_("The briefing section %s is too long.", key))
            sections[key] = value
        if not sections['abort_criteria']:
            raise TextRejected(_("The briefing gives no abort criteria."))
        known = [_plain(line) for line in sections['hazards'] + payload.get('planner_risks', [])]
        for line in sections['abort_criteria']:
            plain = _plain(line)
            if any(hazard and (hazard in plain or plain in hazard) for hazard in known):
                raise TextRejected(_(
                    "The abort criterion %r repeats a hazard. Abort criteria must be changes "
                    "from the plan, not the current forecast.", line,
                ))
        sections['abort_criteria'] = self._with_launch_by(sections['abort_criteria'], payload)
        text = self._compose_briefing(sections)
        self._check_figures(text, payload)
        return text

    @api.model
    def _validate_summary(self, data, payload):
        paragraphs = []
        for key in SUMMARY_PARAGRAPHS:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise TextRejected(_("The summary paragraph %s is missing or empty.", key))
            paragraphs.append(' '.join(value.split()))
        summary = '\n\n'.join(paragraphs)
        if len(summary) > MAX_SUMMARY_LENGTH:
            raise TextRejected(_("The summary is too long (%s characters).", len(summary)))
        self._check_figures(summary, payload)
        fired = payload['flares']['fired']
        if not any(value == fired and decimals == 0 for value, decimals, _t in _numbers(summary)):
            raise TextRejected(_("The summary does not state the %s flares fired.", fired))
        return summary

    @api.model
    def _with_launch_by(self, criteria, payload):
        """Put the launch-by limit from the record first, and drop any other
        line that states the same time."""
        launch_by = payload.get('launch_by_utc') or (payload.get('conditions_at_alert') or {}).get('window_end_utc')
        if not launch_by:
            return criteria
        clock = launch_by[-5:]  # HH:MM
        return [_("Do not take off after %s UTC.", launch_by)] + [
            line for line in criteria
            if clock not in line and not LAUNCH_BY_PASSES.search(line)
        ]

    # Templates ------------------------------------------------------------

    @api.model
    def _compose_briefing(self, sections):
        blocks = []
        for key, label in BRIEFING_SECTIONS:
            value = sections[key]
            if key in BRIEFING_LISTS:
                body = '\n'.join('• %s' % line for line in value) if value else _("• None identified.")
            else:
                body = value
            blocks.append('%s\n%s' % (label, body))
        return '\n\n'.join(blocks)

    @api.model
    def _briefing_template(self, mission):
        mission = mission.sudo()
        area = mission.area_id
        plane = mission.aircraft_id
        alert = mission.alert_id
        snap = alert.snapshot_id
        grams = mission.flare_product_id.grams_per_flare
        role = _("drone operator") if mission.pilot_id.pilot_role == 'drone_operator' else _("pilot")

        overview = _(
            "%(mission)s: cloud seeding over %(area)s for %(client)s. "
            "Crew: %(pilot)s (%(role)s).",
            mission=mission.name, area=area.name, client=mission.partner_id.name or _("no client"),
            pilot=mission.pilot_id.name, role=role,
        )
        if alert:
            weather = _(
                "Forecast for %(when)s UTC (alert %(alert)s): low cloud %(low)s %%, mid cloud "
                "%(mid)s %%, high cloud %(high)s %%, CAPE %(cape)s J/kg, lifted index %(li)s, "
                "wind %(wind)s km/h from %(dir)s°, gusts %(gusts)s km/h, precipitation "
                "probability %(pp)s %%, freezing level %(fl)s m. Seedability score %(score)s of 100.",
                when=_utc(snap.valid_for), alert=alert.name, low=_num(snap.cloud_cover_low),
                mid=_num(snap.cloud_cover_mid), high=_num(snap.cloud_cover_high), cape=_num(snap.cape),
                li=_num(snap.lifted_index) if snap.lifted_index >= 0 else '-' + _num(-snap.lifted_index),
                wind=_num(snap.wind_speed), dir=_num(snap.wind_direction), gusts=_num(snap.wind_gusts),
                pp=_num(snap.precipitation_probability), fl=_num(snap.freezing_level), score=alert.score,
            )
            run = _(
                "Planned takeoff %(takeoff)s UTC, launch by %(by)s UTC. Seedable window "
                "%(start)s to %(end)s UTC over %(lat)s, %(lon)s, radius %(radius)s km.",
                takeoff=_utc(mission.planned_takeoff), by=_utc(mission.launch_by) or _utc(alert.window_end),
                start=_utc(alert.window_start), end=_utc(alert.window_end),
                lat=_num(area.latitude), lon=_num(area.longitude), radius=_num(area.radius_km),
            )
        else:
            weather = _("No weather snapshot is linked to this mission. Obtain a current forecast before takeoff.")
            run = _(
                "Planned takeoff %(takeoff)s UTC over %(lat)s, %(lon)s, radius %(radius)s km.",
                takeoff=_utc(mission.planned_takeoff), lat=_num(area.latitude),
                lon=_num(area.longitude), radius=_num(area.radius_km),
            )
        load = _(
            "%(aircraft)s (%(type)s, tail %(tail)s) from %(base)s, rack capacity %(rack)s. "
            "Load %(flares)s x %(product)s, %(grams)s g each, %(total)s g of seeding material.",
            aircraft=plane.name, type=dict(plane._fields['aircraft_type'].selection).get(plane.aircraft_type, _("Crewed")),
            tail=plane.tail_number or _("not recorded"), base=mission.base_id.name, rack=plane.rack_capacity,
            flares=mission.flares_planned, product=mission.flare_product_id.name,
            grams=_num(grams), total=_num(mission.flares_planned * grams),
        )
        hazards = [line.lstrip('• ').strip() for line in (mission.plan_risks or '').splitlines() if line.strip()]
        if alert and snap.wind_gusts:
            hazards.append(_("Gusts up to %s km/h forecast.", _num(snap.wind_gusts)))
        abort = self._with_launch_by([
            _("Do not take off if the aircraft, crew or flare load differs from this plan."),
            _("Return to base if the seedable cloud dissipates or conditions exceed the crew's limits."),
        ], self._briefing_input(mission))
        return self._compose_briefing({
            'overview': overview, 'weather': weather, 'seeding_run': run,
            'aircraft_and_load': load, 'hazards': hazards, 'abort_criteria': abort,
        })

    @api.model
    def _summary_template(self, mission):
        mission = mission.sudo()
        plane = mission.aircraft_id
        alert = mission.alert_id
        snap = alert.snapshot_id
        is_drone = plane.aircraft_type == 'drone'
        paragraphs = [_(
            "Mission %(mission)s was flown for %(client)s over %(area)s on %(date)s under contract "
            "%(contract)s. %(aircraft)s took off from %(base)s at %(takeoff)s UTC and landed at "
            "%(landing)s UTC, a flight time of %(hours)s hours, %(crew_phrase)s %(pilot)s.",
            mission=mission.name, client=mission.partner_id.name or _("the client"),
            area=mission.area_id.name, date=mission.takeoff_at.strftime('%d %B %Y'),
            contract=mission.contract_id.name or _("(none)"), aircraft=plane.name,
            base=mission.base_id.name, takeoff=mission.takeoff_at.strftime('%H:%M'),
            landing=mission.landing_at.strftime('%H:%M'), hours=_num(mission.flight_hours),
            crew_phrase=_("operated by") if is_drone else _("flown by"), pilot=mission.pilot_id.name,
        )]
        paragraphs.append(_(
            "%(fired)s of the %(loaded)s %(product)s flares loaded were fired, releasing "
            "%(grams)s g (%(kg)s kg) of seeding material.",
            fired=mission.flares_fired, loaded=mission.flares_planned,
            product=mission.flare_product_id.name, grams=_num(mission.material_released_g),
            kg=_num(mission.material_released_g / 1000.0),
        ))
        if alert:
            paragraphs[-1] += ' ' + _(
                "The flight answered cloud alert %(alert)s. The forecast for %(when)s UTC showed "
                "low cloud at %(low)s %%, mid cloud at %(mid)s %%, CAPE of %(cape)s J/kg and wind "
                "of %(wind)s km/h, a seedability score of %(score)s out of 100.",
                alert=alert.name, when=_utc(snap.valid_for), low=_num(snap.cloud_cover_low),
                mid=_num(snap.cloud_cover_mid), cape=_num(snap.cape), wind=_num(snap.wind_speed),
                score=alert.score,
            )
        readiness = self._readiness(mission)
        if readiness['checks_failed']:
            closing = _(
                "At the last check before launch, %(passed)s of the %(total)s readiness checks passed.",
                passed=readiness['checks_passed'], total=readiness['checks_total'],
            )
        elif readiness['checks_not_applicable']:
            closing = _(
                "Before launch, all %(passed)s applicable readiness checks passed (%(na)s did not apply).",
                passed=readiness['checks_passed'], na=readiness['checks_not_applicable'],
            )
        else:
            closing = _("Before launch, all %s readiness checks passed.", readiness['checks_passed'])
        if mission.rainfall_recorded_at:
            closing += ' ' + _(
                "Observed rainfall over the area from the takeoff hour to %(hours)s hours after "
                "landing was %(mm)s mm (%(source)s).",
                hours=RAINFALL_HOURS_AFTER_LANDING, mm=_num(mission.observed_rainfall_mm),
                source=mission.rainfall_source or _("weather archive"),
            )
        else:
            closing += ' ' + _("Observed rainfall over the area has not been recorded yet.")
        paragraphs.append(closing)
        return '\n\n'.join(paragraphs)

    # Entry points ---------------------------------------------------------

    @api.model
    def _draft(self, purpose, mission, instructions, payload, schema, validate):
        """Ask the model, validate its text. Returns (text or None, log)."""
        data, log = self.env['seeding.ai.service'].generate(
            purpose, instructions, payload, schema, mission=mission, alert=mission.alert_id,
        )
        if data is None:
            return None, log
        try:
            return validate(data, payload), log
        except TextRejected as error:
            log._reject(str(error))
            return None, log

    @api.model
    def _fallback_reason(self, log):
        if log.outcome == 'failed':
            return _("The AI writer was unavailable: %s", log.outcome_reason)
        return _("The AI text was rejected: %s", log.outcome_reason)

    @api.model
    def write_briefing(self, mission):
        """Draft the crew briefing for a ready mission. Never raises for an
        AI problem: the template is used instead."""
        mission.ensure_one()
        payload = self._briefing_input(mission)
        text, log = self._draft('briefing', mission, BRIEFING_INSTRUCTIONS, payload,
                                BRIEFING_SCHEMA, self._validate_briefing)
        source = 'ai' if text else 'template'
        mission.write({
            'briefing': text or self._briefing_template(mission),
            'briefing_source': source,
            'briefing_model': log.model_name if source == 'ai' else False,
            'briefing_fallback_reason': False if source == 'ai' else self._fallback_reason(log),
            'briefing_drafted_at': fields.Datetime.now(),
            'briefing_reviewed_by': False,
            'briefing_reviewed_at': False,
        })
        if source == 'ai':
            body = _("Crew briefing drafted by AI (%s). It needs review before takeoff.", log.model_name)
        else:
            body = _("Crew briefing written from the mission record. %s", mission.briefing_fallback_reason)
        mission.message_post(body=body)
        return text is not None

    @api.model
    def write_briefing_from_record(self, mission):
        """Write the crew briefing from the mission record only, with no AI
        call (used for missions recorded in bulk). It awaits review like any
        other briefing."""
        mission.ensure_one()
        mission.write({
            'briefing': self._briefing_template(mission),
            'briefing_source': 'template',
            'briefing_model': False,
            'briefing_fallback_reason': False,
            'briefing_drafted_at': fields.Datetime.now(),
            'briefing_reviewed_by': False,
            'briefing_reviewed_at': False,
        })
        mission.message_post(body=_("Crew briefing written from the mission record. It needs review before takeoff."))

    @api.model
    def write_summary_from_record(self, mission):
        """Write the client summary from the mission record only, with no AI
        call (used for missions recorded in bulk). It awaits review like any
        other summary."""
        mission.ensure_one()
        mission.write({
            'client_summary': self._summary_template(mission),
            'summary_source': 'template',
            'summary_model': False,
            'summary_fallback_reason': False,
            'summary_drafted_at': fields.Datetime.now(),
            'summary_reviewed_by': False,
            'summary_reviewed_at': False,
        })
        mission.message_post(body=_(
            "Client summary written from the mission record. It appears on the mission report "
            "once approved.",
        ))
        return True

    @api.model
    def write_summary(self, mission):
        """Draft the client summary for a completed mission. It stays off the
        mission report until a person approves it."""
        mission.ensure_one()
        payload = self._summary_input(mission)
        text, log = self._draft('summary', mission, SUMMARY_INSTRUCTIONS, payload,
                                SUMMARY_SCHEMA, self._validate_summary)
        source = 'ai' if text else 'template'
        mission.write({
            'client_summary': text or self._summary_template(mission),
            'summary_source': source,
            'summary_model': log.model_name if source == 'ai' else False,
            'summary_fallback_reason': False if source == 'ai' else self._fallback_reason(log),
            'summary_drafted_at': fields.Datetime.now(),
            'summary_reviewed_by': False,
            'summary_reviewed_at': False,
        })
        if source == 'ai':
            body = _("Client summary drafted by AI (%s). It appears on the mission report once approved.", log.model_name)
        else:
            body = _(
                "Client summary written from the mission record. %s It appears on the mission "
                "report once approved.", mission.summary_fallback_reason,
            )
        mission.message_post(body=body)
        return text is not None
