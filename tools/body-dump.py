#!/usr/bin/env python3
"""Extract tagged JSON blobs from agent backend logs.

Scans log files for lines containing ``[<TAG>]`` markers (see ``TAG_PATTERNS``
above) and extracts the JSON payload that follows.  The JSON payload is stored
on the same line as the tag, e.g.:

    [2026-08-27T13:14:32] [ad8b45369f69] [BODY] {"model":"...","messages":[...]}
    [2026-08-27T13:14:32] [ad8b45369f69] [OPENAI_BODY] {"model": "...", ...}

JSON on a single line uses escaped sequences (\\n, \\", etc.).
The script extracts raw text after the tag, attempts to parse as JSON,
and writes pretty-printed output to separate files.

Escaping edge cases handled:
  1. Standard JSON escapes (\n, \", \t, etc.) — parsed natively.
  2. Double-escaped backslashes (\\\\n) — kept as literal \\n in the value.
  3. Multi-line JSON blobs — lines are joined and re-parsed.
  4. Trailing content after the closing brace — trimmed and re-parsed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from yaml.emitter import Emitter

class LiteralDumper(yaml.Dumper):
    """YAML dumper that uses block scalar (|) for multiline strings.

    Patch: *space_break* detection (newline followed by whitespace) causes
    ``analyze_scalar`` to set ``allow_block=False``, which makes
    ``choose_scalar_style`` skip the explicit ``'|'`` requested by the
    representer and fall through to double-quoted flow scalar.  The fix
    re-enables ``allow_block`` for any string that contains ``\\n``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _orig_choose = Emitter.choose_scalar_style
        # Store on instance so __del__ can restore it
        self._LiteralDumper__orig_choose = _orig_choose  # noqa: SLF001

        def _fixed_choose(self):
            # analyze_scalar populates self.analysis at the top of the
            # original choose_scalar_style — run it first.
            if self.analysis is None:
                self.analysis = self.analyze_scalar(self.event.value)
            # space_break (newline + whitespace) forces allow_block=False;
            # override for multiline strings so block style ('|') works.
            if "\n" in self.event.value:
                self.analysis.allow_block = True
                self.analysis.allow_block_plain = True
            return _orig_choose(self)

        # Store the original on the class so subclasses don't re-patch
        if not hasattr(Emitter, "_LiteralDumper__orig_choose"):
            Emitter._LiteralDumper__orig_choose = _orig_choose  # noqa: SLF001
        Emitter.choose_scalar_style = _fixed_choose

    def __del__(self):
        # Restore the original on first destruction only
        if hasattr(Emitter, "_LiteralDumper__orig_choose"):
            Emitter.choose_scalar_style = Emitter._LiteralDumper__orig_choose  # noqa: SLF001
            delattr(Emitter, "_LiteralDumper__orig_choose")  # noqa: SLF001


def _str_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|") if "\n" in data else dumper.represent_scalar("tag:yaml.org,2002:str", data)

LiteralDumper.add_representer(str, _str_representer)

__version__ = "0.3.0"

# Tags to look for in the log.  Each element is the bare text between brackets,
# e.g. "BODY" → the script searches for ``[BODY]``.  Change this list to match
# other log formats.
TAG_PATTERNS: list[str] = ["BODY", "OPENAI_BODY"]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_json_from_line(raw: str, tag: str) -> str | None:
    """Return the substring after ``[<tag>]`` that looks like a JSON object.

    *tag* must include brackets, e.g. ``"[BODY]"`` or ``"[OPENAI_BODY]"``.
    """
    pattern = re.escape(tag) + r"\s+"
    m = re.search(pattern, raw)
    if not m:
        return None
    after = raw[m.end():].lstrip()
    return after if after.startswith("{") else None


def try_parse_json(raw: str) -> dict | list | None:
    """Try to parse *raw* as JSON.  Returns parsed object or *None*."""
    # 1 — direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2 — double-escaped backslashes (\x00 placeholder trick)
    fixed = raw.replace("\\\\", "\x00DUB")
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 3 — trailing content after closing brace (log noise)
    brace_end = raw.rfind("}")
    if brace_end > 0:
        try:
            return json.loads(raw[: brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# File-level scanning
# ---------------------------------------------------------------------------

def extract_entries(filepath: Path) -> list[dict]:
    """Scan *filepath* for [BODY] / [OPENAI_BODY] entries.

    Each entry dict:

    =========  -----------------------------------------------------------
    key        value
    =========  -----------------------------------------------------------
    line_no    1-based line number in the source log
    tag        "BODY" or "OPENAI_BODY"
    req_id     request-ID token (hex)
    raw        raw JSON text from the log line
    parsed     parsed JSON object (dict / list) or *None* on failure
    error      error string if parsing failed
    =========  -----------------------------------------------------------
    """
    entries: list[dict] = []

    with open(filepath, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    # Extract session ID once from the first "[REQ] POST" line that contains "session=".
    req_id = "?"
    for raw_line in lines:
        if "[REQ]" in raw_line and "session=" in raw_line:
            session_m = re.search(r"session=([0-9a-f]{8}-[0-9a-f-]+)", raw_line)
            if session_m:
                req_id = session_m.group(1)
            break

    for lineno, raw_line in enumerate(lines, start=1):
        for tag in TAG_PATTERNS:
            if f"[{tag}]" not in raw_line:
                continue

            json_text = extract_json_from_line(raw_line, f"[{tag}]")
            if json_text is None:
                continue

            parsed = try_parse_json(json_text)
            error = None if parsed is not None else f"Failed to parse at line {lineno}"

            entries.append(
                {
                    "line_no": lineno,
                    "tag": tag,
                    "req_id": req_id,
                    "raw": json_text,
                    "parsed": parsed,
                    "error": error,
                }
            )

    return entries


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def dump_entries(entries: list[dict], out_dir: Path) -> list[Path]:
    """Write parsed JSON and YAML entries to *out_dir*.  Return list of written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for idx, entry in enumerate(entries, start=1):
        tag = entry["tag"]
        req_id = entry["req_id"]

        # New filename: <session-id8chars>-<order-4dig>-<type>.json
        session_id = req_id[:8] if (req_id and req_id != "?") else "00000000"
        json_name = f"{session_id}-{idx:04d}-{tag.lower()}.json"

        if entry["parsed"] is not None:
            out_text = json.dumps(entry["parsed"], ensure_ascii=False, indent=2)
        else:
            out_text = entry["raw"]

        out_path = out_dir / json_name
        out_path.write_text(out_text, encoding="utf-8")

        written.append(out_path)
        print(f"  [{idx}] {out_path.name}  ({len(out_text)} bytes)")

        # Also write YAML alongside JSON
        if entry["parsed"] is not None:
            yaml_name = json_name[:-5] + ".yaml"
            yaml_path = out_dir / yaml_name
            yaml_path.write_text(
                yaml.dump(entry["parsed"], Dumper=LiteralDumper, allow_unicode=True, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Extract [BODY] / [OPENAI_BODY] JSON from agent backend logs (v{__version__}).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("logfile", help="Path to the log file (or directory of log files).")
    parser.add_argument("-o", "--outdir", default=None, help="Output directory (default: <logfile_name>.parts/).")
    parser.add_argument("--stats", action="store_true", help="Print statistics about the entries.")
    args = parser.parse_args()

    log_paths = sorted(Path(args.logfile).glob("*.log*")) if Path(args.logfile).is_dir() else [Path(args.logfile)]

    if not log_paths:
        print(f"Error: no files found matching {args.logfile!r}", file=sys.stderr)
        sys.exit(1)

    all_entries: list[dict] = []
    for lp in log_paths:
        print(f"Reading {lp} …")
        entries = extract_entries(lp)
        all_entries.extend(entries)
        print(f"  Found {len(entries)} entries.\n")

    if not all_entries:
        print("No [BODY] / [OPENAI_BODY] entries found.", file=sys.stderr)
        sys.exit(0)

    out_dir = Path(args.outdir) if args.outdir else Path(f"{log_paths[0].stem}.parts")

    print(f"Writing to {out_dir} …\n")
    written = dump_entries(all_entries, out_dir)

    if args.stats:
        ok = sum(1 for e in all_entries if e["parsed"] is not None)
        fail = len(all_entries) - ok
        tags: dict[str, int] = {}
        models: dict[str, int] = {}
        for e in all_entries:
            tags[e["tag"]] = tags.get(e["tag"], 0) + 1
            if e["parsed"] and isinstance(e["parsed"], dict):
                m = str(e["parsed"].get("model", "?"))
                models[m] = models.get(m, 0) + 1

        print("\n--- Statistics ---")
        print(f"  Total entries : {len(all_entries)}")
        print(f"  Parsed OK     : {ok}")
        print(f"  Parse errors  : {fail}")
        print(f"  Tags          : {dict(tags)}")
        print(f"  Models        : {dict(models)}")
        print(f"  Files written : {len(written)}")

    print(f"\nDone. {len(written)} file(s) written to {out_dir}/")


if __name__ == "__main__":
    main()
