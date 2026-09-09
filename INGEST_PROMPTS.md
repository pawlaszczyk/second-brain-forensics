# Example Prompts for Ingest with Claude Code

Basic principle: one source per call, never in batch. After each step, briefly
check the result before starting the next step.

## 0. Setup check (once at the start of the session)

```
Read CLAUDE.md in full. Briefly confirm that you understand the rules on citation
requirements, marking inferences, externalizing contradictions, and the append-only log,
before we begin the ingest.
```

## 1. Contacts

```
Read CLAUDE.md as the schema. Process raw/ileapp_report/Contacts.tsv (or the
best actual match under raw/, per CLAUDE.md rule 10 -- e.g. "Address Book.tsv").
Note: don't confuse this with Connections.tsv, if present -- that's cell
tower/Wi-Fi network connection history, not personal contacts, and belongs
to step 6, not here. Read the contacts file in full, briefly summarize the
key findings for me, then: create an entity page in wiki/entities/ for each
relevant person/account (template: wiki/entities/_TEMPLATE_entity.md),
update wiki/index.md, and add an entry to wiki/log.md.
```

## 2. Call list

```
Process raw/ileapp_report/Calls.tsv using the same schema. Link calls to
entity pages already known from step 1, create new entity pages if
unknown numbers appear. Add relevant call times to the timeline
(wiki/timeline.md) with timezone information. Update wiki/log.md.
```

## 3. iMessage / SMS

```
Look for the actual SMS/iMessage message-content export under
raw/ileapp_report/_TSV Exports/ -- there is no file literally named
SMS_iMessage.tsv; use list_dir/grep_raw once, per CLAUDE.md rule 10, to find
the real name. On this case, iLEAPP could only produce two edge-case
artifacts for iOS -- "SMS - Missing ROWIDs.tsv" (a report of gaps in the row
sequence, not message content) and "iOS Message Retention.tsv" (a single
retention-policy setting) -- instead of the actual message database. If
that's what you find (no file with real iOS message text), don't keep
searching: note the gap explicitly in wiki/log.md (one line is enough) and
move straight on to the Android side below, rather than repeating the same
list_dir/grep_raw calls hoping for a different result.

Then process the Android side: raw/aleapp_report/_TSV Exports/sms
messages.tsv and mms messages.tsv (Pixel 7a) -- these do contain real
message content. Pay particular attention to messages marked as
deleted/recalled on either platform — document these explicitly on the
respective app page (wiki/apps/iMessage.md for iOS findings, a separate
wiki/apps/SMS-Android.md for the Pixel) and check whether they contradict
other sources (add to wiki/contradictions.md if applicable). Update the
timeline and entity pages. Update wiki/log.md.
```

## 4. Messaging/social apps (repeat per app)

Note (qwen_ingest_harness.py only): this step is expanded automatically at
runtime into one dedicated sub-step per messaging/social app actually found
under raw/*/_TSV Exports/ (own fresh conversation and full round budget per
app -- see expand_messaging_step/discover_messaging_apps in the harness
script). A single shared conversation covering every app in this case (up
to 20 were found) reliably ran out of round budget partway through and
silently dropped most of them. Running this file's steps manually (e.g. the
Claude Code baseline) does NOT get this expansion -- do it per app by hand,
one call per app, as the template below implies.

```
Process raw/ileapp_report/<App>.tsv. Create or update wiki/apps/<App>.md
(template: wiki/apps/_TEMPLATE_app.md). Link recognized accounts/handles to
existing entity pages or create new ones. Explicitly check for hidden areas,
private/encrypted messages, or deleted content and document findings.
Update the timeline. Update wiki/log.md.
```

## 5. Browser history

```
Process raw/ileapp_report/Browser_<Name>.tsv, including private/incognito history
if available separately. Document both separately in wiki/apps/<Browser>.md.
Check for temporal overlaps with other activity in the timeline.
Update wiki/log.md.
```

## 6. Location and fitness data

```
Process raw/ileapp_report/Locations.tsv or the fitness/health exports. These raw
exports can be very large (thousands of GPS points) -- if a `<name>.SUMMARY.tsv`
file exists next to the raw export in the same folder, read that instead of the
raw file: it groups the raw points into trips/segments (Life360) or per-workout
entries (fitness), with an `orig_line_start`/`orig_line_end` column pointing back
into the untouched original for citations. Cite the original file, using that
line range, not the summary file itself. Only fall back to sampling the raw file
directly if no summary file exists. The summary rows also include
`start_place`/`end_place` columns (nearest known city, resolved offline -- not
street-level precision) -- use these in the wiki text alongside the raw
coordinates, don't just report bare lat/lon numbers. Also process Connections.tsv here if present
(cell tower / Wi-Fi network connection history, not personal contacts) -- it can
corroborate or contradict GPS-based location claims (e.g. a cell tower ID implying
a different area than a GPS point at the same time) and is fair game for
wiki/contradictions.md. Add location/movement data to the timeline with timezone
information. Check for contradictions with other temporally close events (e.g.
chat message from location A, GPS data shows location B). Update wiki/log.md.
```

## 7. Media metadata

```
There is no file literally named Media_Metadata.tsv. Use find_files with a
pattern like "assets.*valid.*location" (not "media" -- the real export's
name doesn't contain that word, so a filename search for "media" will miss
it) to locate the actual photo/asset export with geotags -- on iLEAPP this
is typically named something like "Ph5.2-Assets have valid locations-
SyndPL.tsv" (the exact number/suffix varies by iLEAPP version, per CLAUDE.md
rule 10). Focus on creation times, geotags, and attribution to apps/sources.
For notable files with a geotag, resolve the coordinate to a place name with
the resolve_place tool and include that alongside the raw lat/lon -- don't
leave it as bare numbers. Only carry over notable/relevant findings into the
entity/app pages, not every single file. Update wiki/log.md.
```

## 8. Entity consolidation (post-hoc cleanup, repeat per candidate cluster)

Note (qwen_ingest_harness.py only): this step is expanded automatically at
runtime into one dedicated sub-step per candidate duplicate-entity cluster
the harness finds by comparing wiki/entities/ filenames (own fresh
conversation and full round budget per cluster -- see
expand_entity_consolidation_step/discover_entity_clusters in the harness
script). It exists because a run was observed repeatedly creating
near-duplicate entity pages for the same real-world person under slightly
different filenames (case/hyphen/space variants) instead of recognizing an
existing page, which also broke a number of cross-references. Running this
file's steps manually (e.g. the Claude Code baseline) does NOT get this
expansion; that pipeline did not exhibit this failure mode in the first
place. If the harness finds no candidate clusters at all, this step is a
one-line no-op logged as such.

```
The following existing entity pages were flagged as a likely duplicate
cluster by the harness, based on how similar their filenames are. Read all
of them in full before doing anything else.

<file list, injected by the harness -- each entry marked either CONFIRMED
(filename is an exact formatting variant of another entry in this group --
same name, different case/hyphenation/spacing) or NEEDS VERIFICATION
(filename is only similar, not identical)>

For every NEEDS VERIFICATION entry, compare its actual content (account
IDs, phone numbers, email addresses, self-referential statements, and their
cited raw sources) against the CONFIRMED entries in this group -- never
merge based on filename similarity alone. This case deliberately includes
different real people with confusingly similar names (e.g. the device
owner "ThisIsDFIR" versus a different contact called "ThisIsDFIR Two"), so
a similar name is a reason to check, not a reason to merge.

If two or more pages in this group are the same real-world entity:
- Choose the single best filename per CLAUDE.md rule 11 (most descriptive
  and complete; prefer an existing CONFIRMED page's name over a NEEDS
  VERIFICATION one) as the canonical page. Rewrite it with the merged,
  deduplicated content from every confirmed-duplicate page, adding a
  timestamped correction/addendum section that names which page(s) were
  folded in and cites the specific overlapping evidence that confirmed it
  (e.g. a matching account ID, phone number, or email, with its source).
- There is no delete tool. Do not leave the redundant page(s) as full
  duplicates either -- overwrite each with a short stub noting it was
  merged into the canonical page, with a link to it.
- Use grep_raw with path="wiki" to find every other wiki page (app pages,
  timeline.md, contradictions.md, index.md) referencing the now-redundant
  filename(s), and update those references to point to the canonical page.
- Refresh wiki/index.md so it only links the canonical page, not the
  redundant one(s) (see CLAUDE.md rule 13).

If, after checking real content, a NEEDS VERIFICATION page turns out to be
a different real person despite the similar name, leave it separate. Add a
one-line disambiguation note near the top of both pages ("Not to be
confused with [Other Page](Other-Page.md), a different person with a
similar name") so a future reader doesn't make the same assumption.

Update wiki/log.md.
```

## Queries after completing the ingest

```
Create a chronological summary of all events between [date] and
[date] for [entity], including source citations.
```

```
Which contradictions in wiki/contradictions.md are still unresolved? Summarize them
prioritized by potential forensic relevance.
```

```
Show me all inferences (statements not directly observed) made for [entity],
separated from the observed facts.
```
