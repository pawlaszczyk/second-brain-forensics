# Second Brain for Mobile Forensics

An LLM-agent architecture that turns raw mobile forensic extraction data (iLEAPP/ALEAPP TSV exports) into a structured, cross-referenced, citable Markdown wiki — a durable "case file" an examiner can query, rather than a one-off chat transcript.

This repository is the companion code to the paper *"How to Build a Second Brain for a Digital Investigation — A Case Study"* (Pawlaszczyk, Labudde, Engler, Bodach, Kolouch, Kovář, Spranger, Hummert; Hochschule Mittweida / AMBIS vysoká škola). It contains everything needed to run the pipeline yourself, against either a cloud coding agent or a fully local model: the schema, the ingest instructions, the local-model harness, and a few example wiki pages so you can see the output before running anything.

**Status:** companion to a paper currently in review. Citation details below will be filled in once it's published — the architecture, schema, and code here are already final and usable.

## What this actually is

Three directories, three different trust levels, never mixed:

```
evidence/   the original forensic image — never read or written by the agent
raw/        output of a validated parser (iLEAPP/ALEAPP) — read-only input
wiki/       the only layer the agent writes to — entities, apps, timeline, contradictions
```

`CLAUDE.md` is the schema: a short, plain-text set of rules the agent reads before every step — citation requirements (every claim cites a source file and line/row), an explicit *Observed* vs. *Inference* distinction, externalized contradictions instead of silently picking a winner, and an append-only log. It's addressed to whichever LLM reads it, not bound to one vendor.

`INGEST_PROMPTS.md` is the actual sequence of ingest steps (contacts → calls → messages → messaging/social apps → browser history → location/fitness → media metadata), one source at a time, never batched.

## Two ways to run it

**Cloud (e.g. Claude Code, or any capable coding agent with file tools):** point it at `CLAUDE.md` and work through `INGEST_PROMPTS.md` step by step, one source per turn. This is what the paper's main case study used, and it needs no extra tooling — just an agent with read/write file access scoped to `wiki/`.

**Local (`qwen_ingest_harness.py`):** drives a locally hosted model (tested against Mistral via [LM Studio](https://lmstudio.ai/)'s OpenAI-compatible API) through the same schema and steps, without a live coding-agent session. Built because a locally hosted model can't be driven interactively the same way — it opens one fresh, tool-calling conversation per ingest step, with the wiki on disk (not conversation history) carrying state between them.

The harness does two things beyond a literal reading of `INGEST_PROMPTS.md`, both added after real failures observed while running it:

- **Per-app messaging expansion.** A single shared step covering every messaging/social app in a case reliably ran out of its tool-call round budget partway through and silently dropped most of them. The harness deterministically discovers every messaging app present under `raw/*/_TSV Exports/` and expands that one step into one dedicated sub-step per app, each with its own fresh conversation and full round budget.
- **Entity consolidation.** The local model repeatedly created near-duplicate entity pages for the same real-world identity under slightly different filenames (case/hyphen/space variants) instead of recognizing an existing page. The harness deterministically clusters candidate duplicates by filename similarity — using two tiers (exact formatting-variant matches vs. merely-similar names that need content verification) specifically so it never auto-merges two genuinely different people who happen to have similar-looking names — and runs one dedicated review-and-merge sub-step per cluster.

Neither of these needed any change to the schema itself — both live entirely in the harness's Python, which is the point: `CLAUDE.md` stays a portable, model-agnostic spec, and a weaker/smaller model gets compensating structure in code instead of a different schema.

## Quick start

```bash
# 1. Set up a case directory with this structure:
mycase/
├── evidence/            # original image — set up your own chain-of-custody log; the agent never touches this
├── raw/
│   ├── ileapp_report/    # iLEAPP output, if you have an iOS extraction
│   └── aleapp_report/    # ALEAPP output, if you have an Android extraction
├── wiki/                 # starts empty except for the two templates below
│   ├── entities/_TEMPLATE_entity.md
│   └── apps/_TEMPLATE_app.md
├── CLAUDE.md              # copy from this repo, fill in the Case Metadata block at the top
└── INGEST_PROMPTS.md      # copy from this repo as-is

# 2a. Cloud: open mycase/ in your coding agent of choice, tell it to read CLAUDE.md,
#     then work through INGEST_PROMPTS.md one step at a time.

# 2b. Local: install LM Studio, load a model, start its local server, then:
pip install openai reverse_geocode
python3 qwen_ingest_harness.py --case-dir mycase --model <your-model-id> --dry-run   # sanity check first
python3 qwen_ingest_harness.py --case-dir mycase --model <your-model-id>            # the real run
```

Useful flags on the harness: `--dry-run` prints the parsed/expanded step list without contacting the model; `--only "Discord,Browser history"` (comma-separated, case-insensitive substring match against step headings) reruns just specific steps, e.g. after a timeout; `--limit N` runs only the first N steps; `--max-tool-rounds` and `--request-timeout` are worth raising on slower hardware.

## Example output

`examples/` has a couple of finished wiki pages from the paper's own case study (the public [Digital Corpora](https://digitalcorpora.org/) iOS-17/Android-14 training images — synthetic data built for exactly this kind of research, already fully documented in the paper), one pair produced by the cloud run and one by the local run of the *same* source data, so you can compare output quality directly:

- `examples/cloud-run/` and `examples/local-run/` — `WhatsApp.md` (an app page) and `Joshua-Hickman.md` (an entity page)
- `templates/` — the blank page templates every new entity/app page starts from

The paper reports a fuller quantitative comparison (page counts, cross-reference resolution, schema-compliance rates, timing/token usage) between the two runs — see the *Discussion* section once it's published.

## Requirements

- Python 3.10+
- `openai` Python SDK (talks to LM Studio's OpenAI-compatible local server — no data leaves the machine)
- `reverse_geocode` (offline GPS-to-place-name resolution; no network call)
- For the local path: [LM Studio](https://lmstudio.ai/) (or any OpenAI-compatible local inference server) and enough RAM/VRAM for your chosen model
- For the cloud path: any coding agent with file read/write tools that can be scoped to a directory

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, adapt the schema for your own case types.

## Citing this work

```bibtex
@article{pawlaszczyk2026secondbrain,
  title   = {How to Build a Second Brain for a Digital Investigation --- A Case Study},
  author  = {Pawlaszczyk, Dirk and Labudde, Dirk and Engler, Philipp and Bodach, Ronny
             and Kolouch, Jan and Kov\'a\v{r}, David and Spranger, Michael and Hummert, Christian},
  journal = {Digital Threats - Research an Practice},
  year    = {2026},
  note    = {In review (DTRAP-2026-0205) -- volume/issue/DOI to follow}
}
```

## A note on what's *not* here

The actual case data (the `evidence/` and `raw/` layers, and the full finished wikis) isn't in this repo. The Digital Corpora training images the paper uses are freely available from [digitalcorpora.org](https://digitalcorpora.org/) if you want to reproduce the exact case study; for your own casework, `evidence/` and `raw/` should never leave your own infrastructure in the first place, which is the entire point of the architecture (see the paper's discussion of cloud vs. local deployment for real, evidentially sensitive investigations).
