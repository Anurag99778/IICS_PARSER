# IICS → OIC Migration Analysis Parser

Turns an Informatica IICS export package (`.zip`) into the analysis documents
used for the Oracle Integration Cloud migration:

* **`<taskflow>_Analysis.xlsx`** — *Mapping Details* and *Field level mapping*
  sheets, plus a *Parse Report* sheet listing anything needing review
* **`<taskflow>_Analysis.docx`** — the process document, built on your own
  template, with the mapping diagrams embedded automatically

What used to be a day of digging through the IICS UI is a single command.

```bash
pip install -e .
iics-parser export.zip -o output/
```

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

# Let Claude draft the narrative fields (needs ANTHROPIC_API_KEY)
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
| Sub-task / mapping names | taskflow parameters → `mtTask.json` → `mappingId` |
| In-out parameters, parameter file | `mct_*.MTT/mtTask.json` |
| Source/target connections and objects | mapping graph `dataAdapter` → `cn_*.Connection` |
| Expressions, variables | Expression transformation fields |
| Lookups, filters, router conditions | `lookupConditions`, `readOptions`, group conditions |
| Pre/Post SQL, write operations | target `advancedProperties` / `writeOptions` |
| Flow strings (`src->exp->tgt`) | walking the mapping `links` |
| Mass-ingestion directories, actions | `fit_*.MI_TASK.dat` |
| **Mapping diagrams in the Word doc** | the preview JPEG inside each `.DTEMPLATE` |
| Notification recipients, subjects, bodies | `emailNotificationService` parameters |
| Error handling | taskflow `<catch>` handlers |

### What it cannot know

Business owner, technical owner, criticality, trigger type, environments and
the business-purpose narrative are organisational facts, not metadata. They come
from `overrides.yaml`, or are drafted by Claude with `--ai`. Precedence is:

```
overrides.yaml  >  AI draft  >  parsed value
```

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
enrich/      overrides.yaml, optional Claude drafting
   ↓
render/      rows.py → excel.py · word.py
```

Everything renders from the same intermediate representation, so the Excel and
Word outputs can never disagree. Adding a new output (an OIC target-design doc,
a portfolio-wide complexity report) means adding a renderer, not another parser.

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
python -m pytest        # 44 tests
```

## Requirements

Python 3.9+, `openpyxl`, `python-docx`, `PyYAML`. The `--ai` flag additionally
needs `pip install 'iics-parser[ai]'` and an `ANTHROPIC_API_KEY`; without them
the tool runs exactly the same and leaves the narrative fields to a human.
