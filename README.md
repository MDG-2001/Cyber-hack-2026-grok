# Doppelgänger

Is this hit the customer, or just the same name?

A screening alert says a customer looks like someone on a sanctions, PEP or adverse-media list. Most of those hits are a different person with a similar name. This project decides, for every hit, whether the evidence shows the same person, a different one, or that an analyst still has to look.

It follows the bank's written rules. It does not call a model and it does not need an API key. The same input always produces the same file.

You can start from the Google Sheets workbook (`.xlsx`) or from the JSONL folder in the data room. The workbook's **Start here** tab is the count that matters: **440 alerts and 819 hits**. `output/dev.jsonl` has one line for each of those 819 hits.

## On a Mac

You need Python 3.9 or newer. Check with:

```bash
python3 --version
```

If that fails, install Python from [python.org](https://www.python.org/downloads/).

In Terminal, go to this folder (the one that contains `system` and `README.md`):

```bash
cd ~/Downloads/Cyber-hack-2026-grok
```

Install the one library that reads Excel files:

```bash
python3 -m pip install -r requirements.txt
```

### If you have the spreadsheet

Put `sheet.xlsx` somewhere you can find, then:

```bash
python3 system/convert_xlsx.py ~/Downloads/sheet.xlsx data
python3 system/run data output/dev.jsonl
python3 validate.py --data data --output output/dev.jsonl --score data/practice_labels.jsonl
python3 system/serve.py data output/dev.jsonl
```

Open http://127.0.0.1:8766 in a browser. Leave the last command running while you look.

The page is the live demo. **Demo picks** jumps to one clear, one match and one review. **Run decision** replays that hit step by step. **Test against answers** scores the 50 practice labels. **Try your own** is filled in with John Doe, born 1990, against Jon Doe, born 1969, which clears on the date of birth.

`system/run` also accepts the spreadsheet directly, and converts it for you:

```bash
python3 system/run ~/Downloads/sheet.xlsx output/dev.jsonl
```

### If you have the JSONL folder

That folder is named `dev` in the data room. It already contains `alerts.jsonl`, `hits.jsonl`, and the other files. Skip the convert step:

```bash
python3 system/run path/to/dev output/dev.jsonl
python3 validate.py --data path/to/dev --output output/dev.jsonl --score path/to/dev/practice_labels.jsonl
python3 system/serve.py path/to/dev output/dev.jsonl
```

The organisers run `system/run` on a folder of that shape. Do not commit the spreadsheet or the `data/` folder. They are for event participants only.

## What the commands do

| Command | What you get |
|---|---|
| `system/convert_xlsx.py` | A `data/` folder of JSONL files, one file per sheet |
| `system/run` | `output/dev.jsonl`, one decision per hit |
| `validate.py --score` | How many of the 50 practice hits are right |
| `system/serve.py` | A page that shows an alert, its hits, the decision, and the cited records |

`system/README.md` explains how a decision is reached.

## What is in this repo

```
system/run              the command the organisers call
system/convert_xlsx.py  spreadsheet → JSONL folder
system/screen/          the rules
system/serve.py         the review page
system/README.md        how a decision is made
validate.py             the organiser's checker
requirements.txt        openpyxl, only needed for the spreadsheet
output/dev.jsonl        the 819 development decisions
```
