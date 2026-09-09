#!/usr/bin/env python3
"""
qwen_ingest_harness.py

Reproduces the second-brain ingest workflow (raw/ -> wiki/, following
CLAUDE.md and INGEST_PROMPTS.md) against a LOCAL model served by LM Studio,
so it can be compared against the cloud-model (Claude Code) run described
in the CTAFI2026 paper. Everything except the model backend stays the same:
same case folder, same schema, same prompt sequence.

Design notes
------------
* Persistence lives on disk, not in the chat transcript. Each ingest step
  (one source from INGEST_PROMPTS.md) gets a FRESH conversation: system
  prompt = CLAUDE.md, user prompt = that step's instruction. If the model
  needs to see what's already in wiki/ before writing, it calls the
  `read_file` / `list_dir` tools itself, on demand. This mirrors the
  architecture as described in the paper (the wiki, not the prompt
  history, is the durable artefact of record) and keeps context usage low
  -- important on a 27B local model with a realistic 8-16K token budget.

* Hard safety rule, mirroring the schema's own constraint: this script may
  ONLY write inside <case_dir>/wiki/ (and its own JSONL log). raw/ and
  evidence/ are opened strictly read-only. Any tool call that tries to
  write there, or to escape the case directory via '..', is rejected
  before it reaches the filesystem -- see PathGuard below.

* Qwen3-style models emit <think>...</think> reasoning blocks. These are
  stripped before a message is written back into conversation history so
  they don't consume the (small) local context budget turn after turn.

* Every tool call and a final run summary (wall-clock time, token counts,
  tokens/second) are logged as JSON lines to wiki/_ingest_log_qwen.jsonl,
  so the run can be compared against the cloud run for Table 1 / Table 3.

* The "Messaging/social apps" step (originally one step meant to cover
  every messaging app in the case in a single conversation) is expanded
  at runtime into one dedicated sub-step per app actually present in
  raw/, discovered deterministically by scanning raw/*/_TSV Exports/ --
  see expand_messaging_step/discover_messaging_apps. This replaced a
  single-conversation version observed to read several apps' data but
  only have round budget left to write pages for a couple of them, while
  still reporting the step as "ok". Sub-step count therefore varies by
  case (this is expected, not a bug) -- --dry-run shows the actual
  expansion for a given case before running it for real.

* wiki/log.md and wiki/index.md are no longer trusted to the model's own
  discipline -- both were observed drifting even with explicit CLAUDE.md
  rules telling the model how to handle them (duplicated/reformatted log
  entries, and an index.md that linked to pages that were never created
  while omitting ones that were). The harness now enforces both in code:
  a write_wiki_page("wiki/log.md", ...) call is a no-op (the harness
  appends exactly one canonical entry itself after every step, success or
  failure, from what it actually observed that step do), and a
  write_wiki_page("wiki/index.md", ...) call is checked against a real
  list_dir of wiki/entities/ and wiki/apps/ before it hits disk: links to
  files that don't exist are stripped, and existing entity/app pages that
  aren't linked anywhere in the model's content are appended automatically.
  See Tools.write_wiki_page, Tools._autofix_index and append_log_entry.

Setup
-----
    pip install openai
    pip install reverse_geocode   # optional but recommended: offline GPS ->
                                   # place-name resolution (resolve_place
                                   # tool, and the start_place/end_place
                                   # columns in tmp/summarize_location_data.py
                                   # output). No network call, nothing leaves
                                   # this machine -- the city database ships
                                   # inside the package. Without it, both
                                   # still work, just without place names.

In LM Studio: load the model (prefer the MLX 4-bit build on Apple
Silicon), open the "Developer" tab, start the Local Server. Confirm the
model supports tool/function calling (LM Studio flags this per-model) --
Qwen3-family models do. Default endpoint: http://localhost:1234/v1

Usage
-----
    python3 qwen_ingest_harness.py --case-dir /path/to/case \
        --model qwen3.8-27b --limit 3      # small test run first

    python3 qwen_ingest_harness.py --case-dir /path/to/case \
        --model qwen3.8-27b                # full run

IMPORTANT: this script assumes INGEST_PROMPTS.md splits into steps on
top-level "## " Markdown headings. Check --dry-run output against your
actual file structure before the first real run and adjust STEP_SPLIT_RE
below if your file is organised differently.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

from openai import OpenAI

STEP_SPLIT_RE = re.compile(r"\n(?=##\s)")

# Only "## <number>." headings are real ingest steps (## 0. Setup check ...
# ## 7. Media metadata, per INGEST_PROMPTS.md / CLAUDE.md's ingest order).
# The file also has a title+intro paragraph before the first "## " heading,
# and a trailing "## Queries after completing the ingest" section with
# [placeholder]-style templates meant to be filled in and run manually --
# neither of those is an ingest step, so both must be excluded here.
STEP_HEADING_RE = re.compile(r"^##\s+\d+\.")


# --------------------------------------------------------------------------
# Path safety: only wiki/ is writable; raw/ and evidence/ are read-only;
# nothing may resolve outside the case directory.
# --------------------------------------------------------------------------

class PathGuard:
    def __init__(self, case_dir: Path, extra_readable_files=()):
        self.case_dir = case_dir.resolve()
        self.wiki_dir = (self.case_dir / "wiki").resolve()
        self.raw_dir = (self.case_dir / "raw").resolve()
        self.evidence_dir = (self.case_dir / "evidence").resolve()
        # Explicit allowlist -- NOT "anything under case_dir". A case folder
        # commonly has other things sitting next to raw/evidence/wiki (a
        # reference/ground-truth wiki from another run, backups, scratch
        # files, ...). Those must stay invisible to the model: if it can
        # read a reference answer produced by a different run, that run
        # stops being an independent baseline to compare against.
        #
        # evidence/ is deliberately NOT in this list: CLAUDE.md and README.md
        # both state "Never read or write" for evidence/ as an absolute rule.
        # Enforce it here too instead of relying on the model to honor the
        # prompt -- a real run already read evidence/acquisition_log.md and
        # listed the iLEAPP report folder under evidence/ despite the rule.
        self._readable_roots = [self.wiki_dir, self.raw_dir]
        self._readable_files = {
            (self.case_dir / f).resolve() for f in extra_readable_files
        }

    def _resolve_inside_case(self, rel_path: str) -> Path:
        candidate = (self.case_dir / rel_path).resolve()
        if candidate != self.case_dir and self.case_dir not in candidate.parents:
            raise PermissionError(f"refused: path escapes case directory: {rel_path}")
        return candidate

    def readable(self, rel_path: str) -> Path:
        p = self._resolve_inside_case(rel_path)
        allowed = p in self._readable_files or any(
            p == root or root in p.parents for root in self._readable_roots
        )
        if not allowed:
            raise PermissionError(
                "refused: only raw/, evidence/, wiki/ and the schema/prompts "
                f"files are readable (got: {rel_path})"
            )
        if not p.exists():
            raise FileNotFoundError(rel_path)
        return p

    def writable(self, rel_path: str) -> Path:
        p = self._resolve_inside_case(rel_path)
        if p != self.wiki_dir and self.wiki_dir not in p.parents:
            raise PermissionError(
                f"refused: writes are only permitted under wiki/ (got: {rel_path})"
            )
        return p


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

class Tools:
    def __init__(self, guard: PathGuard, logf):
        self.guard = guard
        self.logf = logf
        # Reset at the start of every step in main() -- what a step
        # actually read/wrote is what append_log_entry() reports, instead
        # of trusting the model to summarize it correctly itself.
        self.step_writes = []
        self.step_raw_sources = []
        # Set whenever read_file truncates something this step -- lets
        # run_ingest_step nudge the model before it accepts a "final
        # answer" that reports counts from a partial excerpt as totals.
        self.step_had_truncated_read = False

    def _log(self, event: str, **fields):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        self.logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.logf.flush()

    def _note_raw_source(self, path: str):
        if path.startswith("raw/") and path not in self.step_raw_sources:
            self.step_raw_sources.append(path)

    def read_file(self, path: str, max_chars: int = 20000) -> str:
        p = self.guard.readable(path)
        self._note_raw_source(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        # A *.SUMMARY.tsv file (see tmp/summarize_location_data.py) is
        # deliberately pre-aggregated to be small and citable in one read --
        # give it a higher ceiling than the general default so a few extra
        # enrichment columns (e.g. resolved place names) don't silently push
        # it back over the truncation line and reintroduce the exact
        # "max tool rounds reached re-reading a truncated file" failure this
        # preprocessing step exists to avoid. Observed: adding start_place/
        # end_place columns pushed Life360's summary from ~16KB to ~21KB,
        # just over the old 20,000-char default.
        effective_max = max(max_chars, 40000) if path.endswith(".SUMMARY.tsv") else max_chars
        truncated = len(text) > effective_max
        if truncated:
            self.step_had_truncated_read = True
            # A bare "...[truncated]" suffix was observed not being enough:
            # a step reported "39 SMS messages" as a complete total from a
            # truncated read of sms messages.tsv, when the real file had
            # 1031 lines -- under 4% of the data, presented with no caveat.
            # Compute the real total (cheap, text is already in memory) and
            # make the warning impossible to miss/skim past.
            total_lines = text.count("\n") + 1
            shown_lines = text[:effective_max].count("\n") + 1
            suffix = (
                f"\n\n[TRUNCATED: this is only the first {shown_lines} of "
                f"{total_lines} lines in this file ({len(text)} chars total, "
                f"showing {effective_max}). This is a PARTIAL SAMPLE, not the "
                f"complete file. Do NOT report counts derived from this "
                f"excerpt as totals (e.g. do not say 'N messages' if you only "
                f"saw the first {shown_lines} lines of {total_lines}) -- say "
                f"'at least N of this type in the first {shown_lines} lines "
                f"({total_lines} total, source truncated)' instead, or use "
                f"grep_raw for an accurate count first.]"
            )
        else:
            suffix = ""
        self._log("read_file", path=path, chars=len(text), truncated=truncated, max_chars=effective_max)
        return text[:effective_max] + suffix

    def list_dir(self, path: str) -> str:
        # Shallow listing (like `ls`), not a recursive tree walk: raw/ holds a
        # full iLEAPP/ALEAPP extraction with tens/hundreds of thousands of
        # files, and rglob() over that took minutes and blew up the model's
        # (already small, 8-16K token) context with a single call.
        p = self.guard.readable(path)
        entries = sorted(
            x.name + ("/" if x.is_dir() else "") for x in p.iterdir()
        )
        self._log("list_dir", path=path, count=len(entries))
        return "\n".join(entries[:500]) if entries else "(empty)"

    def grep_raw(self, pattern: str, path: str = "raw", max_matches: int = 200) -> str:
        target = self.guard.readable(path)
        self._note_raw_source(path)
        try:
            out = subprocess.run(
                ["grep", "-rniE", "--include=*", pattern, str(target)],
                capture_output=True, text=True, timeout=60,
            ).stdout
        except Exception as e:
            out = f"[grep error: {e}]"
        lines = out.splitlines()[:max_matches]
        self._log("grep_raw", pattern=pattern, path=path, matches=len(lines))
        return "\n".join(lines) if lines else "(no matches)"

    def find_files(self, pattern: str, path: str = "raw", max_matches: int = 200) -> str:
        """Recursively search for files whose PATH matches a regex pattern
        (case-insensitive) -- this is the filename-search counterpart to
        grep_raw, which searches file *contents* and cannot help locate a
        file by name/extension. Use this when you know roughly what a file
        is called (or its extension) but not which nested subfolder it's
        in -- list_dir alone only lists one directory level (see its
        docstring), and a step observed burning its entire tool-call
        budget alternating between list_dir on the wrong level and
        grep_raw patterns that matched file contents, not names, without
        ever finding the file.

        Backed by the `find` command, not a Python directory walk: rglob()
        over the raw/ tree (tens/hundreds of thousands of files) took
        minutes and blew up context in earlier testing (see list_dir);
        `find -iregex` over the same tree takes low single-digit seconds
        and this only returns matched paths, not file contents.
        """
        target = self.guard.readable(path)
        self._note_raw_source(path)
        # find -iregex matches the WHOLE path, so a bare substring pattern
        # needs '.*' padding to act like a search -- but only where the
        # caller hasn't already anchored that end with ^ / $ themselves
        # (blindly wrapping a pattern like '\.tsv$' with a trailing '.*'
        # breaks the anchor and matches nothing).
        prefix = "" if pattern.startswith("^") else ".*"
        suffix = "" if pattern.endswith("$") else ".*"
        full_pattern = f"{prefix}{pattern}{suffix}"
        try:
            out = subprocess.run(
                ["find", str(target), "-type", "f", "-iregex", full_pattern],
                capture_output=True, text=True, timeout=60,
            ).stdout
        except Exception as e:
            out = f"[find error: {e}]"
        lines = [l for l in out.splitlines() if l.strip()]
        # Report paths relative to the case directory -- that's what every
        # other tool (read_file, grep_raw, ...) expects to be handed back.
        rel_lines = []
        for l in lines[:max_matches]:
            try:
                rel_lines.append(str(Path(l).relative_to(self.guard.case_dir)))
            except ValueError:
                rel_lines.append(l)
        self._log("find_files", pattern=pattern, path=path, matches=len(rel_lines))
        return "\n".join(rel_lines) if rel_lines else "(no matches)"

    def query_sqlite(self, db_path: str, sql: str, max_rows: int = 200) -> str:
        p = self.guard.readable(db_path)
        self._note_raw_source(db_path)
        if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
            raise PermissionError("refused: only SELECT statements are permitted")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
        finally:
            con.close()
        self._log("query_sqlite", db=db_path, sql=sql, rows=len(rows))
        return json.dumps({"columns": cols, "rows": rows}, default=str, ensure_ascii=False)

    def resolve_place(self, lat: float, lon: float) -> str:
        """Resolve a GPS coordinate to the nearest known place name --
        offline, via the bundled `reverse_geocode` package (a GeoNames
        city database shipped with the package; no network call, nothing
        leaves this machine). Precision is "nearest known city/town", NOT
        street-level -- always keep citing the raw coordinate too, this is
        for readability on top of that, not instead of it.

        The Location/fitness SUMMARY.tsv files (see rule 10) already carry
        resolved start_place/end_place columns for their segments -- this
        tool is for one-off coordinates you encounter elsewhere (e.g. a
        media file's geotag) that weren't pre-resolved.
        """
        try:
            import reverse_geocode
        except ImportError:
            return ("ERROR: the 'reverse_geocode' package isn't installed in "
                    "this Python environment -- run `pip install "
                    "reverse_geocode` and restart the harness. Cite the raw "
                    "coordinate without a place name for now.")
        try:
            result = reverse_geocode.get((float(lat), float(lon)))
        except Exception as e:
            return f"ERROR: could not resolve ({lat}, {lon}): {e}"
        parts = [result.get("city"), result.get("state"), result.get("country_code")]
        label = ", ".join(p for p in parts if p) or "(no match)"
        self._log("resolve_place", lat=lat, lon=lon, result=label)
        return (f"{label} -- nearest known city (offline lookup, NOT "
                f"street-level precision). Raw coordinate: {lat}, {lon}")

    # Markdown bullet lines that are *just* a single link, e.g.
    #   - [Device Owner](entities/Device-Owner.md)
    # This intentionally does not try to parse links embedded in prose --
    # every observed index.md write from the model used this one-link-per-
    # bullet style, and staying narrow avoids false positives on lines
    # that merely mention a filename.
    _INDEX_LINK_LINE_RE = re.compile(r"^(\s*[-*]\s*)\[([^\]]*)\]\(([^)]+)\)\s*$")

    def _resolve_wiki_link(self, link_path: str):
        """Classify a link target found in wiki/index.md content relative
        to wiki/. Returns (category, filename, disk_path), or None for
        anything this check shouldn't touch (external URLs, anchors,
        non-.md targets, or a subpath it doesn't recognise)."""
        link_path = (link_path or "").strip()
        if not link_path or link_path.startswith(("http://", "https://", "#", "mailto:")):
            return None
        norm = link_path
        if norm.startswith("./"):
            norm = norm[2:]
        if norm.startswith("wiki/"):
            norm = norm[len("wiki/"):]
        if not norm.endswith(".md"):
            return None
        wiki_dir = self.guard.wiki_dir
        if norm.startswith("entities/"):
            fname = norm[len("entities/"):]
            return ("entities", fname, wiki_dir / "entities" / fname)
        if norm.startswith("apps/"):
            fname = norm[len("apps/"):]
            return ("apps", fname, wiki_dir / "apps" / fname)
        if "/" in norm:
            return None
        return ("root", norm, wiki_dir / norm)

    def _autofix_index(self, content: str):
        """Code-level enforcement of CLAUDE.md rule 13 ("the index reflects
        reality, never a plan"): a prompt-only version of this rule was
        observed not being followed reliably even when stated explicitly,
        so this checks the model's index.md content against a real listing
        of wiki/entities/, wiki/apps/, and root-level wiki pages (e.g.
        contradictions.md, timeline.md) before anything reaches disk --
        dead links are dropped, and existing pages the model forgot to
        link are appended. Returns (fixed_content, notes_for_the_model)."""
        wiki_dir = self.guard.wiki_dir
        existing = {}
        for cat in ("entities", "apps"):
            d = wiki_dir / cat
            existing[cat] = (
                {f.name for f in d.iterdir() if f.is_file() and f.suffix == ".md"}
                if d.is_dir() else set()
            )
        # Root-level content pages (contradictions.md, timeline.md, etc.) --
        # excludes index.md itself and log.md, which is harness-managed and
        # not meant to be linked from the index.
        existing["root"] = {
            f.name for f in wiki_dir.iterdir()
            if f.is_file() and f.suffix == ".md" and f.name not in ("index.md", "log.md")
        }

        removed = []
        kept_lines = []
        for line in content.splitlines():
            m = self._INDEX_LINK_LINE_RE.match(line)
            if m:
                resolved = self._resolve_wiki_link(m.group(3))
                if resolved is not None:
                    _category, _fname, disk_path = resolved
                    if not disk_path.exists():
                        removed.append(f"{m.group(2)} -> {m.group(3)}")
                        continue
            kept_lines.append(line)

        fixed = "\n".join(kept_lines)
        if content.endswith("\n"):
            fixed += "\n"

        def _link_target(cat, fname):
            return fname if cat == "root" else f"{cat}/{fname}"

        added = []
        for cat in ("entities", "apps", "root"):
            for fname in sorted(existing[cat]):
                if _link_target(cat, fname) not in fixed:
                    added.append((cat, fname))

        if added:
            extra = ["", "## Auto-added by harness (exist on disk, were not linked above)"]
            for cat in ("entities", "apps", "root"):
                cat_added = [f for c, f in added if c == cat]
                if cat_added:
                    extra.append("")
                    extra.append("### Other pages" if cat == "root" else f"### {cat.capitalize()}")
                    for fname in cat_added:
                        title = fname[:-3].replace("-", " ")
                        extra.append(f"- [{title}]({_link_target(cat, fname)})")
            fixed = fixed.rstrip("\n") + "\n" + "\n".join(extra) + "\n"

        notes = []
        if removed:
            notes.append("removed dead link(s): " + "; ".join(removed))
        if added:
            notes.append("auto-added missing link(s): " + ", ".join(
                _link_target(c, f) for c, f in added))
        return fixed, notes

    def write_wiki_page(self, path: str, content: str, mode: str = "write") -> str:
        p = self.guard.writable(path)

        # wiki/log.md is now harness-managed (see append_log_entry, called
        # once per step from main()) -- prompt-level rules asking the model
        # to keep it append-only, non-duplicated and consistently formatted
        # were not being followed reliably in practice. A write attempt
        # here is a no-op rather than an error, so it doesn't cost the step
        # a retry.
        if p == self.guard.wiki_dir / "log.md":
            self._log("write_wiki_page_skipped", path=path, reason="log.md is harness-managed")
            return ("SKIPPED: wiki/log.md is written automatically by the harness "
                    "after every step. Don't write to it yourself -- no action needed.")

        # wiki/index.md is checked against the real directory listing
        # before it's allowed to hit disk -- see _autofix_index. Always a
        # full rewrite of the (corrected) content, regardless of the
        # requested mode: index.md has no meaningful "append" semantics
        # once dead links can be stripped and missing ones added.
        if p == self.guard.wiki_dir / "index.md":
            fixed_content, notes = self._autofix_index(content)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(fixed_content, encoding="utf-8")
            self.step_writes.append(path)
            self._log("write_wiki_page", path=path, bytes=len(fixed_content), mode="write",
                       index_autofix=notes)
            note_text = " | ".join(notes) if notes else "no changes needed"
            return (f"OK: wrote {len(fixed_content)} chars to {path} "
                    f"(auto-checked against wiki/entities/ and wiki/apps/: {note_text})")

        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and p.exists():
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
        self.step_writes.append(path)
        self._log("write_wiki_page", path=path, bytes=len(content), mode=mode)
        return f"OK: wrote {len(content)} chars to {path}"


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file inside the case directory (read-only everywhere, including raw/ and evidence/).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the case directory, e.g. raw/whatsapp/msgstore.txt or wiki/apps/whatsapp.md"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files under a directory inside the case directory (e.g. to see what's already in wiki/).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "grep_raw",
        "description": "Search file CONTENTS for a regex pattern under a read-only directory (default raw/). This does NOT search filenames -- use find_files for that.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search, relative to the case directory. Default 'raw'."},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "find_files",
        "description": "Recursively search for files whose PATH/NAME matches a regex pattern (case-insensitive), under a read-only directory (default raw/). Use this to locate a file by name or extension when you don't know which nested subfolder it's in -- list_dir only lists one directory level at a time, and grep_raw searches file contents, not filenames.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex matched against the file's path (case-insensitive), e.g. 'whatsapp.*\\.tsv' or '\\.tsv$'."},
            "path": {"type": "string", "description": "Directory to search under, relative to the case directory. Default 'raw'."},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "query_sqlite",
        "description": "Run a read-only SELECT query against a SQLite database inside the case directory.",
        "parameters": {"type": "object", "properties": {
            "db_path": {"type": "string"},
            "sql": {"type": "string", "description": "Must start with SELECT."},
        }, "required": ["db_path", "sql"]},
    }},
    {"type": "function", "function": {
        "name": "resolve_place",
        "description": "Resolve a GPS coordinate to the nearest known city/town name, offline (no network call, nothing leaves this machine). City-level precision only, NOT street-level -- always keep citing the raw coordinate alongside the resolved name. The Location/fitness SUMMARY.tsv files already have this pre-computed in their start_place/end_place columns; use this tool for one-off coordinates elsewhere, e.g. a media file's geotag.",
        "parameters": {"type": "object", "properties": {
            "lat": {"type": "number", "description": "Latitude, decimal degrees."},
            "lon": {"type": "number", "description": "Longitude, decimal degrees."},
        }, "required": ["lat", "lon"]},
    }},
    {"type": "function", "function": {
        "name": "write_wiki_page",
        "description": "Write or append a Markdown page. ONLY paths starting with 'wiki/' are accepted; anything else is refused. wiki/log.md is written automatically by the harness after every step -- calling this on wiki/log.md is a no-op, don't bother. wiki/index.md is auto-checked against the real contents of wiki/entities/ and wiki/apps/ before it's saved: links to pages that don't exist yet are dropped, and existing pages you forgot to link are added for you.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the case directory, must start with 'wiki/'."},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["write", "append"]},
        }, "required": ["path", "content"]},
    }},
]


def strip_thinking(text):
    """Remove Qwen3-style <think>...</think> reasoning blocks before a
    message is written back into conversation history."""
    if not text:
        return text
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def run_ingest_step(client, model, tools, messages, max_tool_rounds=15, max_completion_tokens=16384):
    """Runs one step to completion: send the prompt, execute any tool
    calls the model makes, feed results back, repeat until the model
    answers with plain text or the round limit is hit.

    max_completion_tokens bounds each individual round (reasoning + content
    + tool-call JSON all count against it for this API). Without a cap, a
    Qwen3-style reasoning model can spiral into a runaway <think> loop that
    never emits a tool call or a final answer -- observed in testing: one
    round accumulated 7000+ reasoning tokens over 10+ hours, visibly going
    in circles, with no timeout ever tripping because tokens kept trickling
    in just fast enough to look "alive" to the HTTP client.

    Also nudges the model once, mid-step, before accepting a "final answer"
    that would otherwise silently end the step having written nothing to
    wiki/ -- two failure signatures observed in testing, both of which
    looked identical to a clean "ok" step in the log until someone checked
    wiki/ by hand: (a) the model read a file that got truncated and reported
    a count from that excerpt as if it were the complete total (39 SMS
    reported as the total when the real file had 1031 lines); (b) the
    model's "final answer" was actually a stray JSON blob -- a malformed
    tool call that never got sent as an actual function call. See
    classify_step_answer's ok_no_pages_written status for the backstop that
    still flags it if this nudge doesn't fix it.
    """
    totals = {"prompt": 0, "completion": 0}
    nudged = False

    for _ in range(max_tool_rounds):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=TOOL_SPECS, tool_choice="auto",
            max_tokens=max_completion_tokens,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            totals["prompt"] += usage.prompt_tokens or 0
            totals["completion"] += usage.completion_tokens or 0

        choice = resp.choices[0]
        msg = choice.message
        clean_content = strip_thinking(msg.content)

        if choice.finish_reason == "length" and not msg.tool_calls:
            # Cut off by the cap above with nothing usable produced yet --
            # most likely the runaway-reasoning-loop pattern described above.
            # Don't accept a truncated ramble as "the answer": bail out of
            # this step instead so it's visible in the log, not silently
            # written into the wiki.
            return (
                f"[round hit max_completion_tokens={max_completion_tokens} "
                "without a tool call or final answer -- likely a runaway "
                "reasoning loop; check the log]",
                totals,
            )

        if not msg.tool_calls:
            looks_like_stray_tool_call = False
            if clean_content:
                try:
                    parsed = json.loads(clean_content)
                    looks_like_stray_tool_call = isinstance(parsed, dict)
                except (ValueError, TypeError):
                    pass
            nothing_written_yet = not tools.step_writes
            if not nudged and nothing_written_yet and (
                looks_like_stray_tool_call or tools.step_had_truncated_read
            ):
                nudged = True
                if looks_like_stray_tool_call:
                    nudge = (
                        "That last response looks like a malformed tool call "
                        "(valid JSON, but it wasn't sent as an actual function "
                        "call) rather than a final answer -- nothing has been "
                        "written to wiki/ yet this step. If you meant to call "
                        "a tool, call it properly now. Otherwise, write your "
                        "findings to wiki/ now, or state in one line why "
                        "there's nothing citable."
                    )
                else:
                    nudge = (
                        "Before finishing this step: you read a file that got "
                        "truncated (see the '[TRUNCATED: ...]' note above), "
                        "but haven't written anything to wiki/ yet. Any counts "
                        "from that excerpt are undercounts, not totals -- "
                        "write your findings now with an explicit caveat (e.g. "
                        "'at least N of this type in the excerpt, source "
                        "truncated, true total unknown') instead of stating "
                        "the excerpt's count as if it were complete. If "
                        "nothing is genuinely citable, say so in one line."
                    )
                messages.append({"role": "assistant", "content": clean_content})
                messages.append({"role": "user", "content": nudge})
                continue
            messages.append({"role": "assistant", "content": clean_content})
            return clean_content, totals

        messages.append({
            "role": "assistant",
            "content": clean_content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments or "{}")
                result = getattr(tools, name)(**fn_args)
            except Exception as e:
                result = f"ERROR: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)[:8000]})

    return "[max tool rounds reached without a final answer -- check the log]", totals


def split_steps(prompts_text: str):
    parts = [s.strip() for s in STEP_SPLIT_RE.split(prompts_text) if s.strip()]
    return [p for p in parts if STEP_HEADING_RE.match(p)]


# --------------------------------------------------------------------------
# Step 4 ("Messaging/social apps") expansion
# --------------------------------------------------------------------------
#
# The original INGEST_PROMPTS.md step 4 is a single "repeat per app"
# instruction meant to be handled in ONE conversation covering every
# messaging/social app in the case. In testing this failed silently: a run
# read WhatsApp, Telegram, Facebook Messenger, Discord and Slack data (all
# five show up in that step's tool-call log), but the 15-round budget only
# stretched to actually WRITING pages for two of them (Discord, Slack) --
# the step still reported status "ok" because *something* real was
# written, so neither existing safety net caught it (the in-loop nudge and
# ok_no_pages_written both key off a step producing NOTHING, not a step
# that partially finishes a multi-item task). A directory scan of that
# case's raw/ turned up 20 distinct messaging/social apps with TSV
# exports -- far beyond what any single conversation's round budget can
# reliably get through.
#
# The fix: discover the app list deterministically (no model call needed)
# by scanning raw/*/_TSV Exports/ for files that look like real message/
# chat content, then replace the single step 4 with one dedicated
# sub-step PER APP, each getting its own fresh conversation and full round
# budget -- exactly the same "give each unit of work its own budget"
# principle already applied to Location/fitness pre-aggregation (see
# tmp/summarize_location_data.py), just at the step-granularity instead of
# the data-granularity. See discover_messaging_apps, build_messaging_app_
# steps and expand_messaging_step below.

# Report-type suffixes that mark a _TSV Exports file as actual message/
# chat CONTENT for some app. Deliberately narrow: "Contacts"/"Calls"/
# "History" alone are NOT included here, because those suffixes also
# belong to files already owned by other steps (Address Book, Google
# Voice - Calls, Safari Browser - History, ...) -- a file only anchors a
# new app if it looks like actual message/chat content. Once an app is
# anchored this way, the sibling sweep in discover_messaging_apps pulls in
# its other report types (Contacts, Call History, Account, ...) too.
_MESSAGE_CONTENT_SUFFIXES = [
    "One To One Messages", "Group Messages", "Chat History",
    "Messages", "Message", "Chats", "Chat",
]

# Apps already fully owned by an earlier/different ingest step -- excluded
# here even if a file would otherwise anchor them, so the same source
# isn't processed twice under two different step numbers. life360chat:
# Life360's own data (including its in-app chat) is step 6's job, not
# step 4's -- CLAUDE.md/INGEST_PROMPTS.md step 6 already owns wiki/apps/
# Life360.md.
_MESSAGE_APP_EXCLUDE_SLUGS = {"sms", "mms", "imessage", "googlevoice", "life360chat"}

# App-name candidates short/generic enough to also be the start of many
# UNRELATED exports from the same vendor (observed: "Google Messages.tsv"
# anchors the app name "Google", and a blind prefix sweep for files
# starting with "Google " then also swept in Google Drive/Photos/Maps/
# Play/Search-History exports that have nothing to do with messaging).
# For these specific anchors only, the sibling sweep in discover_
# messaging_apps requires the remainder after the app name to be a
# recognised report-type suffix (see _SIBLING_SUFFIXES) instead of a
# blind prefix match. Every other (non-generic) app name keeps the
# broader sweep, since a real app name like "WhatsApp" or "Facebook
# Messenger" isn't at meaningful risk of this collision.
_GENERIC_APP_NAME_SLUGS = {"google", "apple", "microsoft", "meta", "samsung", "amazon"}

# Recognised sibling report-type suffixes, used only for the restricted
# (generic-app-name) sweep above. Not meant to be exhaustive -- each
# per-app sub-step also gets one find_files check to catch anything this
# list misses (see build_messaging_app_steps); this just grounds the
# common cases so the model doesn't have to search for them.
_SIBLING_SUFFIXES = [
    "Call History", "Call Logs", "Call Log", "Calls", "Call",
    "User Accounts", "User Account", "User Profile", "User Data",
    "User Information", "User ID", "Users", "User",
    "Contacts", "Contact", "Account", "Accounts", "Settings",
    "Channel Data", "Voicemails", "Voicemail", "Subscription",
    "Numbers", "Number", "Blocked Users", "Friends", "Posts",
    "RealMojis", "Pinned Memories", "Chat List", "Known Users",
    "Replied Messages", "Group Information", "Group Info", "Shared Locations",
    "Local Account", "Pending Uploads",
]
_SIBLING_SUFFIXES_LOWER = {s.lower() for s in _SIBLING_SUFFIXES}


def _slugify_app_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _strip_report_suffix(stem: str, suffixes, sep: str):
    """If stem ends with SEP + one of suffixes, return the app-name
    prefix; else None. Longest suffix wins first so e.g. 'One To One
    Messages' isn't chopped down to just 'Messages'."""
    for suffix in sorted(suffixes, key=len, reverse=True):
        tail = sep + suffix
        if len(stem) > len(tail) and stem.lower().endswith(tail.lower()):
            return stem[: -len(tail)].strip()
    return None


def _is_child_app_name(child_name: str, parent_name: str) -> bool:
    """True if child_name is really just parent_name plus a ' '/'-'-
    separated continuation, e.g. 'Burner Cache' is a child of 'Burner',
    'TikTok - Replied' is a child of 'Tiktok'. Catches an app-name
    candidate that independently matched the anchor pattern but is really
    a more specific report-type variant of an already-anchored app,
    instead of letting it become its own spurious one-file "app"."""
    child_l, parent_l = child_name.lower(), parent_name.lower()
    if child_l == parent_l:
        return False
    return any(child_l.startswith(parent_l + sep) for sep in (" - ", " "))


def discover_messaging_apps(case_dir: Path):
    """Deterministically finds which messaging/social apps have TSV
    exports in this case's raw/ tree, and every related file for each one
    -- see the module comment above this function for why. Returns an
    ordered dict {app_display_name: [relative_paths, ...]}, in first-seen
    order. Scans every raw/*/_TSV Exports/ folder found (not hardcoded to
    ileapp_report/aleapp_report -- a future case may only have one, or a
    differently-named platform folder), so this generalises across cases
    instead of being tuned to one.
    """
    from collections import OrderedDict

    apps: "OrderedDict[str, list]" = OrderedDict()
    anchor_info = {}  # slug -> (display_name, separator_that_matched)

    export_dirs = sorted(case_dir.glob("raw/*/_TSV Exports"))
    if not export_dirs:
        export_dirs = sorted(case_dir.glob("raw/*_report/_TSV Exports"))

    all_tsvs = []
    for d in export_dirs:
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() == ".tsv":
                all_tsvs.append(f)

    # Pass 1: anchor -- find files that look like real message/chat
    # content. Dash form ("<App> - Messages.tsv") is tried before bare-
    # space form ("<App> Messages.tsv") since it's less ambiguous.
    for f in all_tsvs:
        stem = f.stem
        for sep in (" - ", " "):
            app_name = _strip_report_suffix(stem, _MESSAGE_CONTENT_SUFFIXES, sep)
            if app_name:
                slug = _slugify_app_name(app_name)
                if slug and slug not in _MESSAGE_APP_EXCLUDE_SLUGS:
                    anchor_info.setdefault(slug, (app_name, sep))
                    rel = str(f.relative_to(case_dir))
                    apps.setdefault(app_name, [])
                    if rel not in apps[app_name]:
                        apps[app_name].append(rel)
                break

    # Merge spurious child anchors into their more general parent (see
    # _is_child_app_name) -- shortest display names are tried as parents
    # first, so a candidate merges into the most general match available.
    ordered_slugs = sorted(anchor_info, key=lambda s: len(anchor_info[s][0]))
    merged_into = {}
    for i, slug_b in enumerate(ordered_slugs):
        name_b = anchor_info[slug_b][0]
        for slug_a in ordered_slugs[:i]:
            if slug_a in merged_into:
                continue
            if _is_child_app_name(name_b, anchor_info[slug_a][0]):
                merged_into[slug_b] = slug_a
                break
    for slug_b, slug_a in merged_into.items():
        name_b, _sep_b = anchor_info[slug_b]
        name_a, _sep_a = anchor_info[slug_a]
        for rel in apps.get(name_b, []):
            apps.setdefault(name_a, [])
            if rel not in apps[name_a]:
                apps[name_a].append(rel)
        apps.pop(name_b, None)
        del anchor_info[slug_b]

    # Pass 2: sweep in each anchored app's sibling report files (same
    # app-name prefix, across both platforms). Generic app names (see
    # _GENERIC_APP_NAME_SLUGS) use the restricted, allowlisted match;
    # every other app uses a broad prefix match, which also catches
    # oddly-suffixed real variants (e.g. "Facebook Messenger - Chats -
    # msys_database.tsv") that a strict allowlist would miss.
    for f in all_tsvs:
        stem = f.stem
        stem_lower = stem.lower()
        for slug, (display_name, sep) in anchor_info.items():
            if slug in _GENERIC_APP_NAME_SLUGS:
                prefix = (display_name + sep).lower()
                if stem_lower.startswith(prefix):
                    remainder = stem[len(prefix):].strip()
                    if remainder.lower() in _SIBLING_SUFFIXES_LOWER:
                        rel = str(f.relative_to(case_dir))
                        apps.setdefault(display_name, [])
                        if rel not in apps[display_name]:
                            apps[display_name].append(rel)
                    break
                continue
            prefix_dash = (display_name + " - ").lower()
            prefix_space = (display_name + " ").lower()
            if (stem_lower.startswith(prefix_dash) or stem_lower.startswith(prefix_space)
                    or stem_lower == display_name.lower()):
                rel = str(f.relative_to(case_dir))
                apps.setdefault(display_name, [])
                if rel not in apps[display_name]:
                    apps[display_name].append(rel)
                break

    return apps


def build_messaging_app_steps(case_dir: Path, template_step: str):
    """Turns the original single '## 4. Messaging/social apps (repeat per
    app)' step text into one concrete sub-step per app discover_
    messaging_apps() finds, each naming its exact source file(s) up front
    (closing off the same "model has to guess/search for the right
    filename" risk already fixed for steps 3 and 7 -- see
    INGEST_PROMPTS.md) instead of leaving discovery to the model at all.

    template_step's ORIGINAL instruction body (link accounts to entity
    pages, check for hidden/deleted content, update the timeline, ...) is
    reused verbatim for every sub-step -- it stays defined in one place
    (INGEST_PROMPTS.md), not duplicated here.
    """
    apps = discover_messaging_apps(case_dir)
    if not apps:
        # No messaging apps detected in this case -- keep this visible as
        # one step (CLAUDE.md rule 12: every step leaves a log entry)
        # rather than silently vanishing from the run.
        return [
            "## 4. Messaging/social apps (repeat per app)\n\n"
            "No messaging/social app TSV exports were found under raw/*/"
            "_TSV Exports/ in this case (searched for files whose name "
            "ends in a message/chat-content pattern, e.g. '<App> - "
            "Messages.tsv' or '<App> Chats.tsv'). Note this explicitly in "
            "wiki/log.md as 'no messaging/social apps present in this "
            "extraction' -- do not invent findings."
        ]

    body = "\n".join(template_step.splitlines()[1:]).strip()
    total = len(apps)
    sub_steps = []
    for idx, (app_name, files) in enumerate(apps.items(), 1):
        files_list = "\n".join(f"- {f}" for f in sorted(files))
        heading = f"## 4.{idx} Messaging/social apps -- {app_name} ({idx}/{total})"
        prompt = (
            f"{heading}\n\n"
            f"App: {app_name}\n\n"
            f"Source file(s) for this app, located deterministically by the "
            f"harness by scanning raw/*/_TSV Exports/ -- use these exact "
            f"paths, no need to search for them:\n{files_list}\n\n"
            f"If you suspect there may be additional related files not "
            f"listed above (e.g. a deleted/cached variant, or a "
            f"differently-named export), you may run one find_files check "
            f"with a pattern based on \"{app_name}\" to confirm -- but "
            f"don't spend more than one call on that; the list above is "
            f"the primary source of truth for this step.\n\n"
            f"{body}"
        )
        sub_steps.append(prompt)
    return sub_steps


def expand_messaging_step(case_dir: Path, steps: list) -> list:
    """Replaces the single '## 4. Messaging/social apps (repeat per app)'
    step with one dedicated sub-step per app discover_messaging_apps()
    finds (see the module comment above this section for the failure this
    fixes and build_messaging_app_steps for how each sub-step is built).
    Every other step passes through unchanged."""
    expanded = []
    for step in steps:
        heading = step.splitlines()[0]
        if re.match(r"^##\s+4\.\s", heading) and "messaging" in heading.lower():
            expanded.extend(build_messaging_app_steps(case_dir, step))
        else:
            expanded.append(step)
    return expanded


# --------------------------------------------------------------------------
# Step 8 ("Entity consolidation") expansion
# --------------------------------------------------------------------------
#
# Added after observing the local model repeatedly create near-duplicate
# entity pages for the same real-world identity under slightly different
# filenames (case/hyphen/space variants) instead of recognising an
# existing page and updating it -- see the "cloud vs. local" comparison
# in the paper. This also broke a number of cross-references (a page
# linking to entities/thisisdfir.md when the real file was filed as
# ThisIsDFIR.md or ThisIs-DFIR.md).
#
# Mirrors the messaging-app expansion above: deterministic discovery in
# code, one dedicated sub-step (fresh conversation, full round budget)
# per candidate duplicate cluster, instead of one shared step trying to
# review every entity page at once.
#
# Two-tier discovery, computed from filenames only (never file content,
# so it stays cheap, deterministic, and safe to run unattended):
#   - "confirmed": entity filenames that normalize (lowercase, strip
#     everything but letters/digits) to the EXACT same string -- these
#     are guaranteed pure formatting variants of the identical name
#     string, never a false merge across two different real names.
#   - "review": a filename not in any confirmed cluster, but similar
#     (via difflib ratio or containment) to a confirmed cluster's name
#     -- NOT auto-merged, only flagged for the model to check against
#     real page content before deciding. This corpus deliberately uses
#     similar-looking names for different real people (e.g. "ThisIsDFIR"
#     the device owner vs. "ThisIsDFIR Two", a different contact), so
#     cluster-to-cluster similarity is intentionally never compared --
#     only singleton-vs-cluster and singleton-vs-singleton -- which
#     keeps two already-distinct confirmed clusters from ever being
#     proposed as the same entity by this discovery step itself.
# "Unattributed-*"/"Unbekannt-*" fallback names (CLAUDE.md rule 11) are
# excluded from the review tier entirely: they're already distinguished
# by their own number/email and deliberately share a generic prefix that
# would otherwise look "similar" to every other one of them.
_ENTITY_FALLBACK_PREFIXES = ("unattributed", "unbekannt")
_ENTITY_SIMILARITY_THRESHOLD = 0.55
_ENTITY_MIN_CONTAINMENT_LEN = 4


def _normalize_entity_name(stem: str) -> str:
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def _entity_is_fallback(name: str) -> bool:
    return name.lower().startswith(_ENTITY_FALLBACK_PREFIXES)


def _entities_are_similar(a_norm: str, b_norm: str) -> bool:
    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    contains = a_norm in b_norm or b_norm in a_norm
    return ratio >= _ENTITY_SIMILARITY_THRESHOLD or (
        contains and min(len(a_norm), len(b_norm)) >= _ENTITY_MIN_CONTAINMENT_LEN
    )


def discover_entity_clusters(case_dir: Path):
    """Returns an OrderedDict mapping a cluster label to
    {"confirmed": [filenames...], "review": [filenames...]} -- one entry
    per candidate duplicate-entity group (size >= 2 members total).
    Singletons with no candidate match at all are omitted entirely, so an
    empty result means "no candidate duplicates found", not "no entity
    pages exist". See the module comment above for the two-tier discovery
    approach and why it's safe against the cloud/local corpus's
    deliberately similar-looking-but-different real identities."""
    from collections import OrderedDict

    entities_dir = case_dir / "wiki" / "entities"
    if not entities_dir.is_dir():
        return OrderedDict()

    files = sorted(
        f.stem for f in entities_dir.glob("*.md")
        if not f.name.startswith("_TEMPLATE")
    )

    groups = OrderedDict()
    for name in files:
        groups.setdefault(_normalize_entity_name(name), []).append(name)
    confirmed = OrderedDict((k, v) for k, v in groups.items() if len(v) > 1)
    confirmed_members = {n for v in confirmed.values() for n in v}
    singletons = [n for n in files if n not in confirmed_members]

    cluster_review = OrderedDict((k, []) for k in confirmed)
    handled_singletons = set()

    for s in singletons:
        if _entity_is_fallback(s):
            continue
        ns = _normalize_entity_name(s)
        for k, members in confirmed.items():
            if _entity_is_fallback(members[0]):
                continue
            if _entities_are_similar(ns, k):
                cluster_review[k].append(s)
                handled_singletons.add(s)
                break

    # Standalone singleton-vs-singleton similarity, unrelated to any
    # confirmed cluster -- rare, but handled the same way: its own
    # candidate group, every member marked "review" (nothing "confirmed"
    # to anchor it, so the model must verify from scratch).
    remaining = [
        s for s in singletons
        if s not in handled_singletons and not _entity_is_fallback(s)
    ]
    used = set()
    standalone_groups = []
    for i, s in enumerate(remaining):
        if s in used:
            continue
        ns = _normalize_entity_name(s)
        group = [s]
        for s2 in remaining[i + 1:]:
            if s2 in used:
                continue
            if _entities_are_similar(ns, _normalize_entity_name(s2)):
                group.append(s2)
                used.add(s2)
        if len(group) > 1:
            used.add(s)
            standalone_groups.append(group)

    result = OrderedDict()
    for k, members in confirmed.items():
        result[k] = {"confirmed": members, "review": cluster_review.get(k, [])}
    for idx, group in enumerate(standalone_groups):
        result[f"_standalone_{idx}"] = {"confirmed": [], "review": group}
    return result


def build_entity_consolidation_steps(case_dir: Path, template_step: str):
    """Turns the original single '## 8. Entity consolidation (post-hoc
    cleanup, repeat per candidate cluster)' step text into one concrete
    sub-step per candidate duplicate-entity group discover_entity_
    clusters() finds, each naming its exact member pages and whether they
    are a CONFIRMED formatting-variant match or NEED VERIFICATION against
    real content.

    template_step's ORIGINAL instruction body (how to merge, how to
    handle a page that turns out to be a different real person, updating
    cross-references, ...) is reused verbatim for every sub-step -- it
    stays defined in one place (INGEST_PROMPTS.md), not duplicated here.
    """
    clusters = discover_entity_clusters(case_dir)
    body = "\n".join(template_step.splitlines()[1:]).strip()
    if not clusters:
        heading = template_step.splitlines()[0]
        return [
            f"{heading}\n\n"
            "No candidate duplicate-entity clusters were found by the "
            "harness's filename-based discovery (no two entity page "
            "filenames in wiki/entities/ normalize to the same or a "
            "similar string). Add one line to wiki/log.md noting this "
            "check ran and found nothing -- no wiki changes needed."
        ]

    total = len(clusters)
    sub_steps = []
    for idx, (_key, group) in enumerate(clusters.items(), 1):
        confirmed = group["confirmed"]
        review = group["review"]
        file_lines = []
        for name in confirmed:
            file_lines.append(
                f"- wiki/entities/{name}.md  [CONFIRMED formatting-variant "
                f"match: this filename normalizes to exactly the same "
                f"string as the other CONFIRMED entries in this group]"
            )
        for name in review:
            file_lines.append(
                f"- wiki/entities/{name}.md  [NEEDS VERIFICATION: filename "
                f"is only similar, not identical -- confirm from actual "
                f"page content before treating this as the same person]"
            )
        files_list = "\n".join(file_lines)
        heading = f"## 8.{idx} Entity consolidation -- candidate cluster ({idx}/{total})"
        prompt = (
            f"{heading}\n\n"
            f"Candidate duplicate-entity group flagged by the harness "
            f"(filename-based discovery):\n{files_list}\n\n"
            f"{body}"
        )
        sub_steps.append(prompt)
    return sub_steps


def expand_entity_consolidation_step(case_dir: Path, steps: list) -> list:
    """Replaces the single '## 8. Entity consolidation (post-hoc cleanup,
    repeat per candidate cluster)' step with one dedicated sub-step per
    candidate duplicate-entity group discover_entity_clusters() finds
    (see the module comment above this section for the failure this
    fixes and build_entity_consolidation_steps for how each sub-step is
    built). Every other step passes through unchanged."""
    expanded = []
    for step in steps:
        heading = step.splitlines()[0]
        if re.match(r"^##\s+8\.\s", heading) and "entity consolidation" in heading.lower():
            expanded.extend(build_entity_consolidation_steps(case_dir, step))
        else:
            expanded.append(step)
    return expanded


def classify_step_answer(answer: str, tools: "Tools" = None):
    """The two placeholder strings run_ingest_step can return without
    raising an exception (round limit exhausted; a single round burned its
    whole token budget without a tool call or final answer) used to be
    invisible in the JSONL log -- only visible by cross-referencing stdout
    against the log by hand. Give them their own status so a future audit
    doesn't need that.

    Also flags a step that read real raw/ data (tools.step_raw_sources is
    non-empty -- not just a list_dir probe) but never called
    write_wiki_page, even though it "ended cleanly" (no exception, no round
    exhaustion). run_ingest_step already nudges the model once to try to
    fix this in-loop (see there); this is the backstop for when that nudge
    doesn't work, or wasn't triggered because the failure had no
    truncated-read/stray-JSON signature. Note this also fires for a
    legitimate "genuinely nothing citable" step -- that's fine, it's meant
    to prompt a quick look at the log's Summary text, not to claim the step
    failed outright.
    """
    if answer and answer.startswith("[max tool rounds reached"):
        return "max_tool_rounds_exhausted"
    if answer and answer.startswith("[round hit max_completion_tokens"):
        return "runaway_reasoning_cutoff"
    if tools is not None and not tools.step_writes and tools.step_raw_sources:
        return "ok_no_pages_written"
    return "ok"


def append_log_entry(guard: PathGuard, logf, *, heading: str, status: str,
                      pages: list, sources: list, detail: str = ""):
    """Harness-managed replacement for the model writing wiki/log.md by
    hand (write_wiki_page turns that call into a no-op -- see there).
    Appends exactly one entry per step, always in the same format, built
    from what the step actually did (Tools.step_writes / step_raw_sources)
    rather than from what the model claims it did. Called once per step
    from main(), for both the success and the exception path, so a step
    that produced nothing citable still leaves a visible trace instead of
    silently having no entry at all (CLAUDE.md rule 12)."""
    log_path = guard.wiki_dir / "log.md"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if not log_path.exists():
        log_path.write_text(
            "---\n"
            "type: Log\n"
            "title: \"Ingest Log\"\n"
            "description: \"Append-only log of ingest steps (harness-managed)\"\n"
            "tags: [log]\n"
            f"timestamp: \"{ts}\"\n"
            "---\n\n"
            "# Ingest Log\n",
            encoding="utf-8",
        )
    pages_text = ", ".join(pages) if pages else "none -- see note"
    sources_text = ", ".join(sources) if sources else "none recorded"
    summary = detail.strip() if detail and detail.strip() else (
        "Step completed with no tool-recorded output." if status == "ok"
        else f"Step did not complete cleanly (status: {status})."
    )
    entry = (
        f"\n### {ts} -- {heading}\n"
        f"- **Status:** {status}\n"
        f"- **Source(s):** {sources_text}\n"
        f"- **Summary:** {summary}\n"
        f"- **Pages created/updated:** {pages_text}\n"
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    logf.write(json.dumps({
        "ts": ts, "event": "log_entry_appended", "heading": heading,
        "status": status, "pages": pages, "sources": sources,
    }, ensure_ascii=False) + "\n")
    logf.flush()


def main():
    ap = argparse.ArgumentParser(description="Local-LLM second-brain ingest harness (LM Studio)")
    ap.add_argument("--case-dir", required=True, help="Case root containing raw/, wiki/, CLAUDE.md, INGEST_PROMPTS.md")
    ap.add_argument("--model", default="qwen3.8-27b", help="Model identifier as shown by GET /v1/models on your LM Studio server")
    ap.add_argument("--base-url", default="http://localhost:1234/v1")
    ap.add_argument("--prompts-file", default="INGEST_PROMPTS.md")
    ap.add_argument("--schema-file", default="CLAUDE.md")
    ap.add_argument("--log-file", default="wiki/_ingest_log_qwen.jsonl")
    ap.add_argument("--max-tool-rounds", type=int, default=15)
    ap.add_argument("--max-completion-tokens", type=int, default=16384,
                     help="Token cap per round (reasoning + content + tool-call JSON). "
                          "Guards against a runaway reasoning loop that never emits a "
                          "tool call or final answer -- observed: 7000+ reasoning tokens "
                          "over 10+ hours on a single round with no timeout tripping.")
    ap.add_argument("--limit", type=int, default=None, help="Only run the first N steps (use this for the initial dry run)")
    ap.add_argument("--only", default=None,
                     help="Comma-separated substrings (case-insensitive); only run "
                          "steps whose heading contains at least one of them. For a "
                          "targeted follow-up run instead of a full restart, e.g. "
                          '--only "Discord,Browser history,Media metadata"'
                          ". Combine with --dry-run first to confirm the match.")
    ap.add_argument("--dry-run", action="store_true", help="Print the parsed steps and exit, without contacting the model")
    ap.add_argument("--request-timeout", type=float, default=2800.0,
                     help="Per-request timeout in seconds for the LM Studio call. "
                          "The openai SDK default (~600s) is too short for a local "
                          "20B+ model doing tool calls on a memory-constrained Mac -- "
                          "a single round already took 741s for the simplest step "
                          "in testing.")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    guard = PathGuard(case_dir, extra_readable_files=[args.schema_file, args.prompts_file])
    guard.wiki_dir.mkdir(parents=True, exist_ok=True)

    schema_text = (case_dir / args.schema_file).read_text(encoding="utf-8")
    prompts_text = (case_dir / args.prompts_file).read_text(encoding="utf-8")
    steps = split_steps(prompts_text)
    # Replace the single "Messaging/social apps" step with one dedicated
    # sub-step per app actually present in this case -- see
    # expand_messaging_step for why (a single shared conversation/round-
    # budget for every messaging app was observed silently dropping most
    # of them).
    steps = expand_messaging_step(case_dir, steps)
    # Same rationale as expand_messaging_step, applied to entity pages
    # instead of app pages: fold the single "Entity consolidation"
    # step into one dedicated sub-step per candidate duplicate-entity
    # cluster the harness finds in wiki/entities/.
    steps = expand_entity_consolidation_step(case_dir, steps)
    if args.only:
        needles = [n.strip().lower() for n in args.only.split(",") if n.strip()]
        filtered = []
        for s in steps:
            heading = s.splitlines()[0].lstrip("#").strip().lower()
            if any(n in heading for n in needles):
                filtered.append(s)
        print(f"--only={args.only!r} matched {len(filtered)}/{len(steps)} step(s).")
        steps = filtered
    if args.limit:
        steps = steps[: args.limit]

    if args.dry_run:
        print(f"Parsed {len(steps)} step(s) from {args.prompts_file}:\n")
        for i, s in enumerate(steps, 1):
            print(f"--- step {i} ({len(s)} chars) ---")
            print(s[:300], "\n")
        return

    client = OpenAI(base_url=args.base_url, api_key="lm-studio", timeout=args.request_timeout)
    log_path = guard.writable(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as logf:
        tools = Tools(guard, logf)
        run_totals = {"prompt": 0, "completion": 0}
        t0 = time.time()

        for i, step in enumerate(steps, 1):
            print(f"\n=== Step {i}/{len(steps)} ===")
            # Reset per-step tracking: append_log_entry() below reports
            # what this step actually did (Tools records it directly),
            # not what the model says it did.
            tools.step_writes = []
            tools.step_raw_sources = []
            tools.step_had_truncated_read = False
            heading = step.splitlines()[0].lstrip("#").strip()
            # Fresh context per step: the wiki on disk is the durable state,
            # not the chat history. The model pulls in whatever existing
            # wiki/ pages it needs via read_file/list_dir.
            messages = [
                {"role": "system", "content": schema_text},
                {"role": "user", "content": step},
            ]
            # One flaky/slow round (timeout, connection drop, ...) must not
            # take the whole multi-hour, multi-step run down with it -- log
            # the failure and move on to the next step instead of crashing.
            try:
                answer, tok = run_ingest_step(
                    client, args.model, tools, messages,
                    args.max_tool_rounds, args.max_completion_tokens,
                )
            except Exception as e:
                err_text = str(e)
                # "Model unloaded" / "No models loaded" means LM Studio has
                # dropped the model entirely (idle timeout, or the Mac slept
                # mid-run) -- observed in testing: once this hits, every
                # remaining step fails identically and instantly, burning
                # through the rest of the run for nothing. Stop instead of
                # limping through guaranteed failures.
                model_unloaded = (
                    "model unloaded" in err_text.lower()
                    or "no models loaded" in err_text.lower()
                )
                print(f"ERROR: step {i} failed ({type(e).__name__}: {e})"
                      + (" -- stopping run" if model_unloaded else " -- skipping to next step"))
                logf.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "event": "step_error", "step": i, "error_type": type(e).__name__, "error": err_text,
                    "aborted_run": model_unloaded,
                }, ensure_ascii=False) + "\n")
                logf.flush()
                append_log_entry(
                    guard, logf, heading=heading, status=type(e).__name__,
                    pages=tools.step_writes, sources=tools.step_raw_sources,
                    detail=f"Step raised {type(e).__name__}: {err_text}",
                )
                if model_unloaded:
                    print(f"Model appears unloaded in LM Studio -- reload it and rerun "
                          f"(steps {i}-{len(steps)} did not run). Aborting rather than "
                          f"burning through the remaining {len(steps) - i + 1} step(s) "
                          f"with the same guaranteed failure.")
                    break
                continue
            run_totals["prompt"] += tok["prompt"]
            run_totals["completion"] += tok["completion"]
            print((answer or "")[:2000])
            status = classify_step_answer(answer, tools)
            if status != "ok":
                # Previously silent: the harness returned a placeholder
                # string without raising, so this was only visible by
                # eyeballing stdout. Now it's a first-class JSONL event.
                logf.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "event": "step_incomplete", "step": i, "reason": status,
                    "detail": (answer or "")[:500],
                }, ensure_ascii=False) + "\n")
                logf.flush()
            append_log_entry(
                guard, logf, heading=heading, status=status,
                pages=tools.step_writes, sources=tools.step_raw_sources,
                detail=(answer or "")[:500],
            )

        elapsed = time.time() - t0
        summary = {
            "event": "run_summary",
            "steps": len(steps),
            "elapsed_seconds": round(elapsed, 1),
            "prompt_tokens": run_totals["prompt"],
            "completion_tokens": run_totals["completion"],
            "tokens_per_second": round(run_totals["completion"] / elapsed, 2) if elapsed else None,
        }
        logf.write(json.dumps(summary) + "\n")
        print("\n=== Run summary ===")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
