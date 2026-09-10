# IICS → OIC Migration Analysis Parser

Turns an Informatica IICS export package (`.zip`) into the analysis documents
used for the Oracle Integration Cloud migration:

* **`<taskflow>_Analysis.xlsx`** — *Mapping Details*, *Field Values*,
  *Source & Target Fields* and *Connection Details* sheets, plus a *Parse
  Report* with a coverage table
* **`<taskflow>_Analysis.docx`** — the process document, built on your own
  branded template: its cover page, header artwork and contents block are kept
  and its front tables filled in, with the mapping diagrams embedded

What used to be a day of digging through the IICS UI is a single command — or a
single upload, if you deploy the web front end.

```bash
pip install -e .
iics-parser export.zip -o output/
```

## Deploy the web app

One command on any Linux box with Docker. Nothing else to install.

```bash
git clone <this repo> && cd IICS_PARSER
docker compose up -d --build
```

Then open **`http://<server-ip>/parserIICS`**, drop in a `.zip` export package
and download the Excel and Word documents. Several packages can be uploaded at
once, and the page reports the coverage figure for each so you can see at a
glance whether an analysis is whole.

Settings all have working defaults; copy `.env.example` to `.env` to change any
of them:

| | |
|---|---|
| `HTTP_PORT` | host port nginx binds (default `80`) |
| `URL_PREFIX` | the path the app is served under (default `/parserIICS`) |
| `MAX_UPLOAD_MB` | largest upload accepted (default `200`) |
| `JOB_TTL_HOURS` | how long generated documents stay downloadable (default `6`) |
| `OPENAI_API_KEY` | enables the "draft with AI" checkbox; optional |
| `OPENAI_MODEL` | which GPT model to use (default `gpt-4o`) |

Uploads and generated documents are deleted once they pass `JOB_TTL_HOURS` — an
export package contains connection detail that should not sit on a disk
indefinitely.

```bash
docker compose logs -f          # watch it run
docker compose down             # stop
docker compose up -d --build    # after a git pull
```

**Already running something on port 80?** Set `HTTP_PORT=8080` in `.env`, or
drop the `web` service and point your existing nginx at the app container —
`deploy/nginx.conf` is the location block to copy. The app handles the
`/parserIICS` prefix itself, so it works whether your proxy forwards the full
path or strips the prefix first.

> **If `iics-parser` is "not recognized" (common on Windows):** pip installs the
> command into a `Scripts` directory that is often not on PATH. Use the module
> form instead — identical arguments, no PATH needed:
>
> ```bash
> python -m iics_parser export.zip -o output/
> ```

## Usage

```bash
# Every example below also works as `python -m iics_parser ...`

# One package
iics-parser tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001.zip -o output/

# A whole folder of exports - one bad package cannot stop the rest
iics-parser exports/ -o output/

# Only the spreadsheet
iics-parser export.zip --format excel

# Let GPT draft the narrative fields (needs OPENAI_API_KEY)
iics-parser export.zip --ai

# Scaffold the fields a human has to supply
iics-parser export.zip --init-overrides overrides.yaml
iics-parser export.zip --overrides overrides.yaml

# Dump the parsed model as JSON (useful for diffing runs or debugging)
iics-parser export.zip --dump-ir
```

## What is derived automatically

Almost everything. The export package is far more structured than it looks:

| Output | Comes from |
|---|---|
| Step order, names, types | `tf_*.TASKFLOW.xml` — **the link graph**, not document order |
| Error paths and the faults they raise | taskflow `<catch>` and `<throw>` (code, reason) |
| **Decisions vs parallel paths**, with the branch conditions | container `type` + branch `<condition>` |
| Runtime environment (secure agent) per step | taskflow `serviceInput` |
| Sub-task / mapping names | taskflow parameters → `mtTask.json` → `mappingId` |
| In-out parameters, parameter file | `mct_*.MTT/mtTask.json` |
| Source/target connections and objects | mapping graph `dataAdapter` → `cn_*.Connection` |
| Expressions, variables | Expression transformation fields |
| **Column lineage** (`o_proj_unit → SEGMENT_1`) | target `manualMappings` |
| **Source/target field grids** (name, type, precision, scale, origin) | `dataAdapter.object.fields` |
| **Sort keys with direction, aggregator group-by keys** | `sortEntries`, `sortFields`, `groupByFieldsList` |
| Ticked designer checkboxes | `advancedProperties`, read/write options |
| Lookups (incl. SQL overrides), filter and **router group conditions** | `lookupConditions`, `filterConditions`, `groupFilterConditions` |
| Pre/Post SQL, write operations, update strategy | target `advancedProperties` / `writeOptions` |
| Schema-qualified objects, flat-file layout | `object.dbSchema`, `object.fileAttrs` |
| Flow strings (`src->exp->tgt`) | walking the mapping `links` |
| Mass-ingestion directories, file patterns, retention | `fit_*.MI_TASK.dat` |
| Every file-operation property (rename suffix, PGP key, …) | `taskActions[].properties`, read generically |
| **Mapping diagrams in the Word doc** | the preview JPEG inside each `.DTEMPLATE` |
| Notification recipients, subjects, bodies | `emailNotificationService` parameters |
| Schema per leg, on the Connection Details sheet | `object.dbSchema`, else the connection's schema/user |
| Error handling | taskflow `<catch>` handlers |

### What it cannot know

Business owner, technical owner, criticality, trigger type, environments and
the integration-purpose narrative are organisational facts, not metadata. They
come from `overrides.yaml`, or are drafted by GPT with `--ai` (or the checkbox
in the web app). Precedence is:

```
overrides.yaml  >  AI draft  >  parsed value
```

The **Integration Purpose/Objective** section is the clearest case: it takes
the taskflow's own description, an override, or the GPT draft — and shows
`[to be confirmed]` when none of those supplied one, rather than letting the
standing migration boilerplate underneath it pass for a purpose.

Anything unfilled appears as a red `[to be confirmed]` in the Word document
rather than being quietly invented, and every AI-drafted field is listed in the
Parse Report so a reviewer knows what to check.

## Overrides

```yaml
defaults:                       # applied to every integration
  business_owner: Finance
  environments: DEV, TEST, UAT, PROD
  upstream_app_owner: Finance Group (Informatica Hub Owner)

integrations:
  tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001:
    business_process_name: Finance Solutions - Concur
    technical_owner: Finance Solutions - Concur
    criticality: Medium
    trigger_type: Schedule
    schedule_note: Daily, around 3:00 AM PDT
```

`--init-overrides` writes one of these pre-populated with exactly the fields
that are still empty for a given package.

## Architecture

```
extract/     unpack the zip (and its nested asset zips), index assets by type
   ↓
parse/       taskflow · mapping graph · tasks · connections  →  build.py
   ↓
model/ir.py  one normalised Integration object   ← the single source of truth
   ↓
enrich/      overrides.yaml, optional GPT drafting
   ↓
render/      rows.py → excel.py · word.py
   ↓
web/         Flask upload form over the same pipeline
```

Everything renders from the same intermediate representation, so the Excel and
Word outputs can never disagree. Adding a new output (an OIC target-design doc,
a portfolio-wide complexity report) means adding a renderer, not another parser.

### Checking an integration you have never seen

Every workbook carries a **Coverage** table in its Parse Report: for each
category of object found in the package, how many reached the output.

```
Coverage                      100% of objects found in the package appear here
  Taskflow steps              24/24 — complete
  Mappings reached by a step   9/9 — complete
  Target column mappings      33/33 — complete
  Source/target columns      686/686 — complete
  Sort / group-by keys         9/9 — complete
  ...
```

This is deliberately not the parser's own opinion of how it did — it re-reads
the package and the rendered rows and compares them, so a whole category going
missing is visible even when no individual step reported a problem. Anything
short of complete names the specific objects. Read this first on a new
integration; it is the fastest way to know whether the analysis is whole.

### Packages with no taskflow

Not every integration is an orchestration. IICS exports a single asset as
readily as a whole taskflow, so a package is often just a mapping, or a mapping
task and the mapping it runs. Those are documented the same way — sources,
targets, expressions, column lineage, field grids and the mapping diagram all
come out — with one difference the Parse Report states plainly: with no
taskflow there is no execution order, so the assets are listed in **name
order**, which must not be read as the order they run in.

### Handling integrations this parser has not seen

Transformation types are resolved through each mapping file's own
`$$classInfo` type table rather than hard-coded class numbers, so a Joiner,
Normalizer or Union in another integration identifies itself correctly. A type
that is genuinely unrecognised is still named, still placed in the flow, and
raised as a warning in the Parse Report — never dropped, never fatal.

The Concur package in `samples/` is the regression baseline. It exercises
mapping tasks, mass ingestion, command tasks, notifications, an assignment,
parallel paths, a decision, unconnected lookups, a router, a hierarchy parser
and pre-SQL. If work to support another integration breaks a test there, that
is the signal to look again.

```bash
python -m pytest        # 116 tests (67 of them need a package in samples/)
```

## Requirements

Python 3.9+, `openpyxl`, `python-docx`, `PyYAML`. The `--ai` flag additionally
needs `pip install 'iics-parser[ai]'` and an `OPENAI_API_KEY`; without them the
tool runs exactly the same and leaves the narrative fields to a human. The web
app needs `pip install 'iics-parser[web]'`, or just use the container.
