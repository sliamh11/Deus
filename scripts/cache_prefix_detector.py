#!/usr/bin/env python3
"""Cache-prefix-stability detector (LIA-205).

Detector-ONLY. Scans an assembled system-prompt prefix for *volatile* tokens
(UUIDs, ISO-8601 dates, JWTs, hex hashes) that silently bust the provider's
KV-cache prefix. The first volatile token caps how much of the prefix a slot
can reuse via longest-common-prefix matching (llama-server's default), so its
offset is the local-path prefill-reuse ceiling.

This never mutates anything. Headroom's `CacheAligner` began as a prompt
*rewriter* and was deliberately reverted to detector-only because rewriting the
cache hot zone breaks the prefix it was trying to protect; we inherit that
lesson by construction. Clean-room reimplementation of the concept (stdlib
only, no code paste); structural parsers, no regex.

Under flat-subscription billing this saves $0 on the Claude path -- the payoff
is local-model (Ollama/llama.cpp) prefill latency and awareness. Never frame
the output as dollars saved.

Not wired to CI or the fcc proxy by design (the proxy is a third-party uv
tool); that wiring is deferred and tracked in LIA-205. The parser core is kept
reusable so a future interceptor can import it.

The single-prefix scan flags volatile *shape*, not confirmed *change*: a
hex-shaped permanent-memory ULID is correctly flagged yet never churns, so it
does not actually bust the cache. The `--diff` mode closes that gap -- it diffs
two real assembled prefixes (earlier vs later request) and reports the offset of
the first char that ACTUALLY differs, the true reuse ceiling; the shaped-token
core becomes a secondary attribution signal.

Usage:
    cache_prefix_detector.py <file>        # analyze a file's content
    cache_prefix_detector.py --stdin       # analyze stdin
    cache_prefix_detector.py <file> --json  # structured output
    cache_prefix_detector.py --diff A B    # true reuse ceiling = first real diff
"""

from __future__ import annotations

import argparse
import base64
import json
import string
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import uuid

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from _exit_codes import SUCCESS, USAGE_ERROR, NOT_FOUND, INTERNAL_ERROR  # noqa: E402
from _agent_io import is_agent_context, compact_json, select_fields  # noqa: E402

# Character classes for the structural parsers (no regex).
_DIGITS = frozenset("0123456789")
_HEX = frozenset("0123456789abcdefABCDEF")
_HEXDASH = _HEX | frozenset("-")
_JWT_CHARS = frozenset(string.ascii_letters + string.digits + "-_.")

_SAMPLE_MAX = 16  # never emit full content -- truncate the sample

# Sanitized-window bounds for `--diff` attribution. `context_before` is the
# shared tail right before the split (identical in both files); a_after/b_after
# are the first differing chars from each side. Same "never echo full content"
# philosophy as _SAMPLE_MAX -- callers pre-slice to these char counts.
_CONTEXT_BEFORE = 48
_CONTEXT_AFTER = 24


@dataclass(frozen=True)
class VolatileToken:
    """A single volatile token found in the prefix.

    `sample` is truncated (<= _SAMPLE_MAX chars) so we never echo full content
    (a prefix can contain secrets); `offset` is the char index in the prefix.
    `length` is the token's full (un-truncated) char span -- kept so `--diff`
    can test whether a divergence offset falls INSIDE a token's span (e.g. a
    date whose last digit changed). It is deliberately NOT serialized by
    `as_dict`, so the existing single-prefix JSON contract is unchanged.
    """

    offset: int
    kind: str  # "uuid" | "iso8601" | "jwt" | "hex"
    sample: str
    length: int

    def as_dict(self) -> dict:
        return {"offset": self.offset, "kind": self.kind, "sample": self.sample}


# ---------------------------------------------------------------------------
# Structural parsers. Each returns (offset, length, kind, raw) candidate tuples;
# `detect_volatile_tokens` merges, sorts, and suppresses overlaps.
# ---------------------------------------------------------------------------


def _runs(text: str, alphabet: frozenset):
    """Yield (start, substring) for each maximal run of chars in `alphabet`."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] in alphabet:
            j = i + 1
            while j < n and text[j] in alphabet:
                j += 1
            yield i, text[i:j]
            i = j
        else:
            i += 1


def _sample(raw: str) -> str:
    return raw if len(raw) <= _SAMPLE_MAX else raw[: _SAMPLE_MAX - 3] + "..."


def _detect_uuids(text: str):
    """Canonical 36-char dashed UUIDs only. The 32-char dashless form is ceded
    to the hex detector (it collides with MD5)."""
    out = []
    for start, run in _runs(text, _HEXDASH):
        if len(run) != 36:
            continue
        try:
            uuid.UUID(run)
        except ValueError:
            continue
        out.append((start, 36, "uuid", run))
    return out


def _detect_iso8601(text: str):
    """`DDDD-DD-DD` date heads, confirmed with `date.fromisoformat` (rejects
    impossible dates like 9999-99-99). A trailing time/offset is irrelevant --
    the date's start is the volatile offset. Compact basic-form dates
    (`YYYYMMDD`, no dashes) are intentionally out of scope -- they are
    indistinguishable from ordinary 8-digit numbers without a delimiter."""
    out = []
    n = len(text)
    i = 0
    while i + 10 <= n:
        head = text[i : i + 10]
        shaped = (
            head[0] in _DIGITS
            and head[1] in _DIGITS
            and head[2] in _DIGITS
            and head[3] in _DIGITS
            and head[4] == "-"
            and head[5] in _DIGITS
            and head[6] in _DIGITS
            and head[7] == "-"
            and head[8] in _DIGITS
            and head[9] in _DIGITS
        )
        if shaped:
            left_ok = i == 0 or text[i - 1] not in _DIGITS
            right_ok = i + 10 >= n or text[i + 10] not in _DIGITS
            if left_ok and right_ok:
                try:
                    date.fromisoformat(head)
                except ValueError:
                    pass
                else:
                    out.append((i, 10, "iso8601", head))
                    i += 10
                    continue
        i += 1
    return out


def _b64url_to_json_obj(seg: str):
    """Decode an unpadded base64url segment to a JSON value, or None."""
    pad = "=" * (-len(seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(seg + pad)
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _detect_jwts(text: str):
    """Shape-only JWTs: exactly 3 non-empty base64url segments where the first
    (header) decodes to a JSON object. The JSON-object check rejects lookalikes
    such as semver `1.2.3` or `a.b.c`. No signature verification."""
    out = []
    for start, run in _runs(text, _JWT_CHARS):
        parts = run.split(".")
        if len(parts) != 3 or not all(parts):
            continue
        if isinstance(_b64url_to_json_obj(parts[0]), dict):
            out.append((start, len(run), "jwt", run))
    return out


def _detect_hex(text: str):
    """Contiguous hex runs of length 32/40/64 (MD5/SHA1/SHA256). The 40-char
    case also matches a git SHA1 -- intentional: a hardcoded commit hash in the
    prefix is volatile too."""
    out = []
    for start, run in _runs(text, _HEX):
        if len(run) in (32, 40, 64):
            out.append((start, len(run), "hex", run))
    return out


def detect_volatile_tokens(text: str) -> list[VolatileToken]:
    """All volatile tokens in `text`, sorted by offset, overlaps suppressed."""
    candidates = []
    candidates.extend(_detect_uuids(text))
    candidates.extend(_detect_iso8601(text))
    candidates.extend(_detect_jwts(text))
    candidates.extend(_detect_hex(text))
    # Deterministic order: offset, then kind, then length.
    candidates.sort(key=lambda c: (c[0], c[2], c[1]))
    # Greedy interval suppression: keep the earliest token in any overlapping
    # span (a token nested inside another should not be double-listed; the
    # ceiling metric only needs the first offset regardless).
    kept = []
    cursor = -1
    for off, length, kind, raw in candidates:
        if off < cursor:
            continue
        kept.append(VolatileToken(off, kind, _sample(raw), length))
        cursor = off + length
    return kept


def analyze_prefix(text: str) -> dict:
    """Reduce the token list to the aggregate cache-reuse ceiling metric.

    `prefix_stable` is ALWAYS present and is the authoritative signal -- a clean
    prefix has `first_volatile_offset = None`, which `compact_json` strips, so
    callers must key off `prefix_stable`, not the (nullable) offset.
    """
    tokens = detect_volatile_tokens(text)
    total_len = len(text)
    if not tokens:
        return {
            "prefix_stable": True,
            "total_len": total_len,
            "first_volatile_offset": None,
            "volatile_tail_fraction": 0.0,
            "tokens": [],
        }
    first = tokens[0].offset
    fraction = (total_len - first) / total_len if total_len else 0.0
    return {
        "prefix_stable": False,
        "total_len": total_len,
        "first_volatile_offset": first,
        "volatile_tail_fraction": round(fraction, 4),
        "tokens": [t.as_dict() for t in tokens],
    }


# ---------------------------------------------------------------------------
# `--diff` mode: the AUTHORITATIVE reuse-ceiling signal. The single-prefix scan
# above flags shape; this diffs two real prefixes for the first char that
# actually CHANGES. Read-only -- compares, never mutates.
# ---------------------------------------------------------------------------


def _first_divergence(a: str, b: str) -> int | None:
    """Index of the first differing char between `a` and `b` (= the longest
    common prefix length), or None if neither diverges within the shared length.

    None covers two cases the caller separates via the lengths: identical
    strings, and one being a strict prefix of the other (no mismatch in
    [0, min(len)), so the common prefix is the whole shorter string). Single
    O(min(len)) scan; never allocates the prefix string."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return None


def _window(raw: str) -> str:
    """Escape control chars in a short context slice so a newline/tab cannot
    corrupt single-line human output. Printable non-ASCII (e.g. Hebrew in the
    vault prefix) is kept as-is -- `unicode_escape` would obscure it. Callers
    pre-slice `raw` to a bounded length (never echo full content, same intent
    as `_sample`)."""
    out = []
    for ch in raw:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif unicodedata.category(ch)[0] == "C":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def diff_prefixes(a: str, b: str) -> dict:
    """Diff two assembled prefixes (FILE_A = earlier/reference, FILE_B =
    later/current) and report the offset of the first char that ACTUALLY
    differs -- the true local KV-cache reuse ceiling, the signal the
    single-prefix `analyze_prefix` heuristic can only approximate by shape.

    `prefixes_identical` is ALWAYS present and authoritative: `compact_json`
    strips the nullable `reuse_ceiling`, so callers must key off the bool.
    Offsets are char indices -- a tight upper bound on the token-level ceiling
    (the token holding the first differing char is the first non-reusable one).

    Attribution is secondary. `detect_volatile_tokens` runs on FILE_A; the
    pre-ceiling region is byte-identical in both files, so the choice of side is
    immaterial. `shaped_false_alarms` counts shaped tokens lying ENTIRELY before
    the divergence -- shaped-but-stable tokens the single-prefix heuristic would
    have flagged but the diff proves reusable (the load-bearing LIA-281 point).
    The divergence is `shaped` iff a token's span covers the offset. Read-only.
    """
    a_len, b_len = len(a), len(b)
    div = _first_divergence(a, b)

    if div is None and a_len == b_len:
        # Identical: the whole prefix is reusable, so EVERY shaped token in it
        # is a false alarm (none of them change).
        false_alarms = sum(1 for t in detect_volatile_tokens(a) if t.offset < a_len)
        return {
            "prefixes_identical": True,
            "a_len": a_len,
            "b_len": b_len,
            "reuse_ceiling": None,
            # b_len==0 guard wins over the identical->1.0 rule: two empty
            # strings reuse nothing meaningful.
            "reuse_ceiling_fraction": 1.0 if b_len else 0.0,
            "one_is_prefix_of_other": False,
            "shaped_false_alarms": false_alarms,
        }

    one_prefix = div is None  # no mismatch within shared length, unequal lengths
    ceiling = min(a_len, b_len) if one_prefix else div

    tokens = detect_volatile_tokens(a)
    # Entirely before the divergence -> reusable -> the heuristic over-counted.
    false_alarms = sum(1 for t in tokens if t.offset + t.length <= ceiling)
    # The shaped token (if any) whose span COVERS the divergence -- the
    # heuristic's correct hit (e.g. a date whose last digit changed).
    hit = next(
        (t for t in tokens if t.offset <= ceiling < t.offset + t.length),
        None,
    )

    return {
        "prefixes_identical": False,
        "a_len": a_len,
        "b_len": b_len,
        "reuse_ceiling": ceiling,
        "reuse_ceiling_fraction": round(ceiling / b_len, 4) if b_len else 0.0,
        "one_is_prefix_of_other": one_prefix,
        "shaped_false_alarms": false_alarms,
        "divergence": {
            "offset": ceiling,
            "shaped": hit is not None,
            "kind": hit.kind if hit else None,
            "context_before": _window(a[max(0, ceiling - _CONTEXT_BEFORE) : ceiling]),
            "a_after": _window(a[ceiling : ceiling + _CONTEXT_AFTER]),
            "b_after": _window(b[ceiling : ceiling + _CONTEXT_AFTER]),
        },
    }


def _print_human(report: dict) -> None:
    if report["prefix_stable"]:
        print(
            f"prefix stable: no volatile tokens in {report['total_len']} chars "
            "-- full prefix is KV-reusable"
        )
        return
    first = report["first_volatile_offset"]
    pct = report["volatile_tail_fraction"] * 100
    print(
        f"reuse ceiling = {first}/{report['total_len']} chars; "
        f"{pct:.1f}% of the prefix is non-reusable once it churns"
    )
    for t in report["tokens"]:
        print(f"  @{t['offset']:>7}  {t['kind']:<8}  {t['sample']}")


def _print_diff_human(report: dict) -> None:
    if report["prefixes_identical"]:
        print(
            f"prefixes identical: full {report['b_len']} chars KV-reusable "
            "(no divergence)"
        )
        return
    ceiling = report["reuse_ceiling"]
    pct = report["reuse_ceiling_fraction"] * 100
    div = report["divergence"]
    kind = div["kind"] or "unstructured"
    # When one prefix is a strict prefix of the other there is no differing char
    # -- the shorter just ran out. Tag it so the empty A/B next line doesn't read
    # like a bug.
    tail = " (one prefix is a prefix of the other)" if report["one_is_prefix_of_other"] else ""
    print(
        f"reuse ceiling = {ceiling}/{report['b_len']} chars "
        f"({pct:.1f}% of FILE_B reusable from FILE_A); "
        f"first real divergence at offset {ceiling} [{kind}]{tail}"
    )
    print(f"  before: {div['context_before']}")
    print(f"  A next: {div['a_after']}")
    print(f"  B next: {div['b_after']}")
    print(
        "  shaped-but-stable tokens before divergence: "
        f"{report['shaped_false_alarms']}"
    )


def _emit(
    report: dict,
    args: argparse.Namespace,
    long_fields: tuple,
    human: Callable[[dict], None],
) -> int:
    """Shared output plumbing for both modes: JSON (with --compact/--select) in
    agent context, else the given human printer. `long_fields` names the fields
    `--compact` may truncate (differs per mode)."""
    if args.json or is_agent_context():
        output = report
        if args.compact:
            output = compact_json(output, long_fields=long_fields)
        if args.select:
            output = select_fields(output, args.select)
        print(json.dumps(output))
    else:
        human(report)
    return SUCCESS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cache_prefix_detector",
        description="Detect volatile tokens that bust the KV-cache prefix (detector-only).",
    )
    parser.add_argument(
        "file", nargs="?", help="Path to a file containing the assembled prefix"
    )
    parser.add_argument(
        "--stdin", action="store_true", help="Read the prefix from stdin instead of a file"
    )
    parser.add_argument(
        "--diff",
        nargs=2,
        metavar=("FILE_A", "FILE_B"),
        help="Diff two prefixes (earlier vs later); report the offset of the "
        "first char that ACTUALLY differs -- the true KV-cache reuse ceiling.",
    )
    parser.add_argument("--json", action="store_true", help="Structured JSON output")
    parser.add_argument(
        "--compact", action="store_true", help="Strip nulls / truncate long fields"
    )
    parser.add_argument(
        "--select", type=str, default=None, help="Comma-separated field paths to include"
    )
    args = parser.parse_args(argv)

    # Exactly one input mode: a file, --stdin, or --diff A B.
    if sum(bool(m) for m in (args.file, args.stdin, args.diff)) != 1:
        print(
            "error: provide exactly one of a file, --stdin, or --diff FILE_A FILE_B",
            file=sys.stderr,
        )
        return USAGE_ERROR

    if args.diff:
        try:
            texts = []
            for raw_path in args.diff:
                path = Path(raw_path)
                if not path.is_file():
                    print(f"error: file not found: {raw_path}", file=sys.stderr)
                    return NOT_FOUND
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            print(f"error: could not read input: {exc}", file=sys.stderr)
            return INTERNAL_ERROR
        report = diff_prefixes(texts[0], texts[1])
        return _emit(
            report,
            args,
            long_fields=("context_before", "a_after", "b_after"),
            human=_print_diff_human,
        )

    try:
        if args.stdin:
            text = sys.stdin.read()
        else:
            path = Path(args.file)
            if not path.is_file():
                print(f"error: file not found: {args.file}", file=sys.stderr)
                return NOT_FOUND
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: could not read input: {exc}", file=sys.stderr)
        return INTERNAL_ERROR

    report = analyze_prefix(text)
    return _emit(report, args, long_fields=("sample",), human=_print_human)


if __name__ == "__main__":
    sys.exit(main())
