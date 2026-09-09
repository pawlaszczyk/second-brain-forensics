# Case Schema — Second Brain for Mobile Forensics

## Case Metadata (fill in before ingest)

- Case ID:
- Device/Image:
- Investigation mandate/question:
- Responsible investigator:
- Date set up:

## Purpose of this knowledge base

This knowledge base supports the triage, correlation, and documentation of a mobile
forensic investigation. It does not replace the investigator's professional judgment,
but prepares material so that connections become visible faster and nothing is
overlooked.

## Directories and their rules

- `evidence/` — Original exhibit. **Never read or write.**
- `raw/` — Output of a validated parsing tool (e.g. iLEAPP). **Read only, never modify.**
- `wiki/` — The only layer you write to.

## Mandatory rules for every ingest step

1. **One source per step.** Always process exactly one file/artifact category from
   `raw/`, never several in a batch. Briefly discuss key findings before updating the wiki.
2. **Citation requirement.** Every statement in `wiki/` must reference the source file
   and line/timestamp/offset, e.g. `(Source: raw/ileapp_report/sms.tsv, line 482)`.
3. **Mark inference.** Explicitly distinguish between
   `Observed:` (directly readable from the source) and `Inference:` (concluded by the agent).
   Never mix inferences with facts without marking them.
4. **Externalize contradictions, don't resolve them.** If two sources contradict each other
   (e.g. deleted vs. recovered message, differing timestamps), this is documented
   in `wiki/contradictions.md` — not silently decided in favor of one source.
5. **Always carry timezones.** Every timestamp in `wiki/timeline.md` includes the
   source's original timezone or UTC offset. When device or OS versions change
   during acquisition, explicitly flag possible time shifts.
6. **OKF frontmatter mandatory.** Every new wiki page begins with a YAML frontmatter block:
   ```yaml
   ---
   type: Forensic Entity | Forensic App | Timeline | Contradiction | Index
   title: "Page name"
   description: "One-line context"
   tags: [relevant keywords]
   timestamp: "YYYY-MM-DDTHH:MM:SSZ"   # time of creation or last ingest update
   ---
   ```
   The `type` field is mandatory (OKF specification), all other fields recommended.
   The frontmatter must be the very first thing in the file -- the first line
   is exactly `---` with nothing before it (no stray character, no blank
   line). Double-check this after writing; a corrupted first line breaks
   every OKF-compliant parser reading this wiki.
7. **Cross-references as standard Markdown links.** Cross-link entities, apps, and
   timeline entries: `[Page name](../entities/Page-name.md)` instead of `[[Page name]]`.
   Obsidian understands both formats; standard Markdown links ensure OKF compatibility
   and interoperability with external tools (e.g. Google OKF Visualizer).
8. **Append-only log.** `wiki/log.md` is now written by the harness itself,
   automatically, once per step -- not by you. A `write_wiki_page` call
   targeting `wiki/log.md` is a no-op (this was made a hard guarantee at the
   code level after append-only/format-consistency were not being followed
   reliably from the prompt alone). Don't spend a tool-call round on it.
9. **Name uncertainty.** If a source is unclear, incomplete, or ambiguous,
   this is noted explicitly on the respective wiki page rather than smoothed over.
10. **Efficient file lookup.** If the exact file path named in an instruction does not
    exist (raw/ export filenames vary by tool/version and rarely match a step's example
    name literally), do not deliberate over it at length: use `find_files` once to
    locate the best actual match by filename under `raw/` (it searches recursively by
    path/name, unlike `list_dir`, which only lists one directory level, and unlike
    `grep_raw`, which searches file *contents*, not filenames -- using `grep_raw` to
    hunt for a filename or extension is a common way to burn a step's whole tool-call
    budget without ever finding the file). Note the real filename you used in your
    citations, and proceed. If genuinely ambiguous between multiple plausible candidates,
    pick the most directly relevant one, process it, and name the alternatives as a
    caveat on the page rather than re-deriving the same uncertainty repeatedly.
    A raw export can be too large to read in one call (it gets cut off with
    "...[truncated]"); re-reading it again returns the identical truncated
    text, so don't retry it hoping for more. If a `<name>.SUMMARY.tsv`
    companion file exists next to it in the same folder, read that instead --
    it condenses the raw rows into citable segments/groups with an
    `orig_line_start`/`orig_line_end` pointer back into the original, which is
    what you cite. Otherwise, sample the file (e.g. head/tail via grep) rather
    than looping on repeated full reads. The location/fitness SUMMARY.tsv
    files already include `start_place`/`end_place` columns (nearest known
    city, resolved offline -- not street-level) -- use those directly in the
    wiki text alongside the raw coordinate. For a GPS coordinate you find
    elsewhere (e.g. a media file's geotag) that isn't pre-resolved, call the
    `resolve_place` tool instead of leaving it as bare numbers.
11. **Descriptive filenames.** Entity and app filenames must say what the page is
    about, not just repeat a raw identifier. Prefer, in this order: a known real name;
    a label derived from an associated account handle/email/username (e.g.
    `joshuahickman957`); a role label inferable from context (e.g. `Device-Owner`);
    or, only when nothing better is available, a labeled fallback that still states
    what the record is, e.g. `Unattributed-Number-919-888-7386.md` rather than a bare
    `919-888-7386.md`. Never use a bare phone number, hash, or file offset alone as
    the entire filename.
12. **Never end a step as a silent no-op.** If you've spent most of the
    step's tool-call budget investigating and are not converging on a wiki
    write, stop investigating with a few calls still in reserve and write
    what you have -- a short, clearly caveatted entry beats nothing. If a
    source genuinely has nothing citable (empty, no new information), still
    append a one-line `wiki/log.md` entry saying so explicitly (e.g.
    "Processed raw/.../Calls.tsv -- no new entities or events found, nothing
    else written") so the gap is visible in the log instead of silently
    absent. A step that ends without any `wiki/log.md` entry at all is a bug.
13. **The index reflects reality, never a plan.** Immediately before writing
    or updating `wiki/index.md`, run `list_dir` on `wiki/entities/` and
    `wiki/apps/` and link only pages that exist on disk right now, under
    their actual filenames. Never add a link for a page you intend to create
    later, expect to create in a future step, or that a step's example
    happens to mention -- if `write_wiki_page` hasn't been called for it yet,
    it does not go in the index yet. A link in `wiki/index.md` to a file that
    doesn't exist is worse than a missing link and counts as a bug. If a step
    creates or renames pages, refreshing the index (re-checked against
    `list_dir`, not from memory) is the last thing you do in that step.
    (The harness also checks this in code before saving `wiki/index.md`:
    dead links get stripped and missing existing pages get added
    automatically. That's a safety net, not a reason to skip the `list_dir`
    check yourself -- getting it right the first time avoids the auto-fix
    silently rearranging your intended structure.)

## Structure of the wiki pages

- `wiki/index.md` — Entry point with links to all entities, apps, and the
  timeline. Refresh it at the end of every step that adds or renames an
  entity/app page -- if a page exists under `wiki/entities/` or `wiki/apps/`
  but isn't linked from here, that's a bug, check before finishing the step.
  Equally a bug the other way: never link a page here that doesn't actually
  exist yet (see rule 13) -- verify with `list_dir`, don't write from memory
  or from what you're planning to create. The harness auto-checks this
  against disk as a backstop (rule 13), but that doesn't replace doing it
  right yourself.
- `wiki/log.md` — written automatically by the harness after every step
  (rule 8). Don't call `write_wiki_page` on it yourself.
- `wiki/entities/<descriptive-name>.md` — one page per account/persona/device;
  filename must be descriptive, not a bare identifier (see rule 11) (template see
  `wiki/entities/_TEMPLATE_entity.md`).
- `wiki/apps/<app>.md` — one page per app/data source (template see
  `wiki/apps/_TEMPLATE_app.md`).
- `wiki/timeline.md` — chronological timeline across all sources.
- `wiki/contradictions.md` — list of open contradictions between sources.

## Ingest order (recommended)

1. Contacts (builds the entity base)
2. Call list
3. iMessage/SMS (iOS message content may not be extractable -- iLEAPP can
   leave only edge-case artifacts like "SMS - Missing ROWIDs.tsv" behind
   instead of real message text; check raw/aleapp_report/_TSV Exports/
   for "sms messages.tsv" / "mms messages.tsv" -- the Pixel side -- as well)
4. Messaging/social apps, one at a time
5. Browser history (incl. private mode)
6. Location and fitness data (incl. Connections.tsv -- cell tower/Wi-Fi
   network history, NOT personal contacts, despite the similar name)
7. Media metadata (last, largest volume, lowest signal-to-noise ratio)

## What the agent should NOT do

- Never create, modify, or delete files under `evidence/` or `raw/`.
- Never formulate final legal or culpability-related assessments — only provide
  observations and clearly marked inferences.
- Never process sources in batch, even if it would be faster.
