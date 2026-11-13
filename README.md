# Serein

**Cloud seeding operations for Odoo 19 Community.**

Serein runs a cloud seeding operator end to end. It watches live weather over each client's target areas and raises an alert when clouds are seedable. A language model proposes a mission: aircraft, crew, flare load, and launch window. Eleven hard rules verify the proposal, and a person decides. Serein then tracks the flight, flare stock, maintenance, observed rainfall, the client report, and the invoice.

> **AI proposes, rules verify, a person decides.** The AI never launches anything. Every proposal must pass the readiness engine and be approved by a person, and every AI call is logged.

## What is AI and what is not

| Part | What it is |
|---|---|
| Mission planner | AI (Google Gemini), with validation and a rule-based fallback |
| Crew briefing and client summary | AI-drafted, checked against the mission record, reviewed by a person |
| Seedability score | A transparent formula, shown on every alert |
| Readiness engine | Eleven hard rules, one method each |

## Features

### Weather and cloud alerts
- An hourly fetch from [Open-Meteo](https://open-meteo.com) for every target area, saved as a weather snapshot with its raw response.
- **Seedability score, 0 to 100:**
  - Cloud: 50 × the larger of low and mid cloud.
  - Instability: 30 × CAPE, capped at 1000 J/kg.
  - Wind: 20 at 20 km/h or less, falling to 0 at 50 km/h.
- An alert is raised when the score reaches the threshold (default 60). Its window lasts while the forecast stays seedable, up to 6 hours.
- **Replay Recorded Conditions** raises a labelled alert from a saved snapshot, for when there is no network.
- Alert sources: live, forecast archive, replayed, and manual.

### Mission planning
- **Plan with AI** on an alert. The model receives the alert, the contract, the fleet, the crew, and the flare stock, and returns a fixed JSON plan with its reasoning and risks.
- Every answer is validated: the aircraft, pilot, and base must exist, the load must fit, and the times must fall inside the window. A failed call or an invalid answer falls back to the rule-based planner, and the mission is marked "Planned by rules".
- **Re-plan** (up to two rounds) sends the failed checks back to the model. It is offered only when an asset check fails.

### Readiness engine
The engine runs on every proposal and change, and again before Ready and Launch. A mission cannot become ready or launched while any check fails.

| # | Check | Passes when |
|---|---|---|
| 1 | Contract active | The contract is active and the flight date is inside its season |
| 2 | Area covered | The target area belongs to the contract |
| 3 | Flight-hour budget | Hours used are below the budget |
| 4 | Alert still valid | The alert has not expired or been dismissed |
| 5 | Aircraft airworthy | No open corrective or scheduled-service request, and under the service interval |
| 6 | Aircraft free and in range | Not on another launched mission, and its base serves the area |
| 7 | Rack capacity | The flares planned fit the aircraft's rack |
| 8 | Pilot licence and base | The licence is valid, matches the aircraft type, and the pilot is based at the aircraft's base |
| 9 | Pilot medical | Valid (not applicable to drone operators) |
| 10 | Pilot duty hours | Under the weekly limit (rolling 7 days) |
| 11 | Flares in stock | The base holds at least the flares planned |

Each check explains its result in plain language. Those explanations are what the AI receives on Re-plan.

### Flight, crew briefing, and client report
- Missions move from Proposed to Ready, Launched, Completed, and Billed (or Cancelled), shown on a status kanban.
- **Crew briefing:** drafted by the AI on Mark Ready, reviewed before takeoff.
- **Client summary:** drafted by the AI on Complete, then edited and approved by a person.
- **Figure check:** a rule rejects any AI text containing a number that is not in the mission record, and a template is used instead.
- **PDF mission report:** weather, readiness results, flares by batch, material released, observed rainfall, and the approved summary. It states that the text was AI-drafted and names the reviewer.

### Fleet, crew, and stock
- **Aircraft:** maintenance equipment with a home base, flare rack, and service interval. Hours since service come from completed missions. Crossing the interval opens a scheduled-service request, and the aircraft stays grounded until it is closed.
- **Pilots and drone operators:** employees with a home base, licence and medical expiry, duty hours, and a weekly limit.
- **Bases:** each one serves a set of target areas and holds its own flare stock.
- **Flares:** tracked by batch and bought from a vendor through automatic reorder rules. Completing a mission releases the flares fired, by batch, to a "Released to Atmosphere" location, and records the grams of material released.
- **Colour-coded statuses:**
  - Aircraft: Airworthy, Service Due Soon, or Grounded.
  - Crew: Cleared, Expiring Soon, or Not Cleared.
  - Contracts: Within Budget, Budget Low, or Budget Used.
  - Snapshots and areas: Seedable, Marginal, or Poor.

### Billing, rainfall, and reporting
- **Create Invoice** on a contract, for a chosen period: standby days at the contract fee, plus the flight hours of completed missions. A mission or a day is never billed twice.
- **Mission cost:** flares, aircraft hours, and crew hours.
- **Fetch Observed Rainfall:** the rainfall over the area from the takeoff hour to 24 hours after landing, from the ERA5 reanalysis. Serein records it and never claims the seeding caused it.
- **Reporting:** graph and pivot views of flight hours by client, material released by area, cost per mission, and missions by planner.
- **AI Activity:** a read-only log of every model call, with the request, the response, the duration, the token counts, and the outcome.

## Requirements
- **Odoo 19.0 Community.**
- **Odoo apps:** Contacts, Discuss, Invoicing, Inventory, Purchase, Maintenance, and Employees. They are installed automatically through the module's dependencies.
- **Python:** `requests`, which ships with Odoo.
- **Network:** outbound HTTPS to `api.open-meteo.com`, `archive-api.open-meteo.com`, `historical-forecast-api.open-meteo.com`, and `generativelanguage.googleapis.com`.
- **A Google Gemini API key** for the AI features. Without one, planning falls back to the rules, and texts are written from templates.

## Installation
1. Put this repository's folder on Odoo's `addons_path` (the folder that contains `serein_ops`).
2. Set the Gemini key in the Odoo server's environment. It is read from `os.environ` and never stored in the database or logs:
   ```
   GEMINI_API_KEY=your-key
   ```
3. Install the module from Apps (after **Update Apps List**), or from the command line:
   ```
   odoo-bin -c odoo.conf -d <database> -i serein_ops
   ```
4. Users get one of two groups under **Serein**: **Operator** (plans and flies missions) or **Manager** (full rights, configuration, invoicing).

## Configuration

| Setting | Where | Default |
|---|---|---|
| Seedability threshold | System parameter `serein_ops.seedability_threshold` | `60` |
| Gemini model | System parameter `serein_ops.gemini_model` | `gemini-3.5-flash-lite` |
| Areas a base serves | Serein → Configuration → Bases → Target Areas Served | none (serves every area) |
| Crew home base | Pilot or drone operator form | none (may fly from any base) |
| Service interval, duty limit | Aircraft form, pilot form | 100 h, 30 h |

A base with no served areas, or a crew member with no home base, is unrestricted. This keeps existing data working while bases and crew are set up.

## Demo world
**Serein → Configuration → Load Demo World** (Managers) fills an empty database with a fictional regional operator and its last 150 days of operations:
- **Clients and fleet:** 6 clients in six countries with 16 target areas, 5 bases, 10 aircraft (3 drones), and 14 crew.
- **History from real weather:** cloud alerts raised from the forecasts that were actually issued for each area (Open-Meteo's historical forecast archive). The missions flown against them went through the readiness engine as it stood before each takeoff. Unflown alerts are dismissed with a reason, or left to expire.
- **Operations behind it:** flare purchases received by batch with paid vendor bills, scheduled services and breakdowns, observed rainfall, client summaries to review, and invoices and payments.
- **Today:** a live weather fetch, plus missions in every open state (one in the air, one ready, one proposed, and one blocked by a failed check).

Loading takes about 1.5 minutes and needs internet. Without it, a smaller history is built from recorded snapshots, without cloud alerts. The loader refuses a database that already holds Serein records, and it renames the company to *Stratus Ridge Weather Services*. All clients and people in it are fictional.

## Running the tests
The tests mock every network call: Gemini and Open-Meteo are never contacted.
```
odoo-bin db -c odoo.conf drop serein_test
odoo-bin -c odoo.conf -d serein_test -i serein_ops --test-enable --test-tags /serein_ops --stop-after-init --http-port 8070
```
Exit code 0 means every test passed. On Windows, run this from PowerShell: Git Bash rewrites the `/serein_ops` tag.

## Repository layout
```
serein_ops/
  models/      weather service, snapshots and alerts, missions, readiness engine,
               AI service, planner and writer, fleet/crew/stock/billing extensions,
               demo world loader and its history scheduler
  views/       forms, lists, kanban, reports, and menus
  wizard/      the contract invoicing wizard
  report/      the PDF mission report
  data/        sequences, crons, system parameters, service products, recorded snapshots
  security/    groups and access rights
  tests/       automated tests (Odoo's standard test framework)
IDEAS.md       ideas for later
```

## Data sources and attribution
- **Weather data by [Open-Meteo.com](https://open-meteo.com) (CC BY 4.0):** the forecast, the historical forecast archive, and the ERA5 reanalysis for observed rainfall. The credit appears on alerts, snapshots, and the mission report. Open-Meteo's free API is for non-commercial use; commercial use needs their paid API.
- **AI by Google Gemini.** On the free tier, prompts may be reviewed by Google, so send only fictional data.

## License
LGPL-3.
