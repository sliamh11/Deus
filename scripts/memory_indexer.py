#!/usr/bin/env python3
"""
Deus Memory Indexer
Semantic search over session logs using sqlite-vec + Gemini embeddings.

Usage:
  python3 memory_indexer.py --add <path/to/session_log.md>
  python3 memory_indexer.py --query "linear algebra exam prep"  [--top 3]
  python3 memory_indexer.py --rebuild
  python3 memory_indexer.py --extract <path/to/session_log.md>
  python3 memory_indexer.py --wander [topic1 topic2 ...]
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

# Allow running as a direct script — add project root to sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import sqlite_vec
from google import genai
from google.genai import types as genai_types

from evolution.config import (
    EMBED_DIM,
    GEN_MODELS,
    load_api_key as _load_api_key,
)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("~/.config/deus/config.json").expanduser()
DB_PATH = Path("~/.deus/memory.db").expanduser()
LAST_RESUME_LEARNINGS = Path("~/.deus/last_resume_learnings.txt").expanduser()


def _load_vault_path() -> Path:
    """Load vault path from config.json or DEUS_VAULT_PATH env var."""
    # 1. Environment variable override
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    # 2. Config file
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if cfg.get("vault_path"):
                return Path(cfg["vault_path"]).expanduser()
        except (json.JSONDecodeError, OSError):
            pass
    # 3. Fatal — no vault configured
    print(
        "ERROR: Memory vault not configured.\n"
        "Set DEUS_VAULT_PATH or add vault_path to ~/.config/deus/config.json\n"
        "Run /setup → 'memory' in Claude Code to configure.",
        file=sys.stderr,
    )
    sys.exit(1)


_vault_root = _load_vault_path()
VAULT_SESSION_LOGS = _vault_root / "Session-Logs"
VAULT_ATOMS = _vault_root / "Atoms"
DEDUP_L2_THRESHOLD = 0.55  # ≈ cosine similarity 0.85 for unit-normalized vectors
# Recency boost for --query --recency-boost (subtracted from L2 distance).
RECENCY_BOOST_7D = 0.3    # last 7 days — strong boost
RECENCY_BOOST_30D = 0.15  # 7-30 days — moderate boost

_client: genai.Client | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_api_key() -> str:
    try:
        return _load_api_key()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def embed(text: str) -> list[float]:
    from evolution.providers.embeddings import embed as _provider_embed
    return _provider_embed(text)


def serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))


# ── DB ────────────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            path     TEXT NOT NULL,
            date     TEXT,
            chunk    TEXT NOT NULL,
            type     TEXT NOT NULL,
            tldr     TEXT,
            topics   TEXT,
            decisions TEXT
        )
    """)
    db.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS embeddings
        USING vec0(embedding float[{EMBED_DIM}])
    """)
    # Backward-compatible: add atom columns if upgrading an existing DB
    for col, definition in [("confidence", "REAL DEFAULT 0.0"), ("corroborations", "INTEGER DEFAULT 0")]:
        try:
            db.execute(f"ALTER TABLE entries ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def entry_exists(db: sqlite3.Connection, path: str) -> bool:
    row = db.execute("SELECT 1 FROM entries WHERE path = ? LIMIT 1", [path]).fetchone()
    return row is not None


def delete_entries(db: sqlite3.Connection, path: str):
    ids = [r[0] for r in db.execute("SELECT id FROM entries WHERE path = ?", [path]).fetchall()]
    for eid in ids:
        db.execute("DELETE FROM embeddings WHERE rowid = ?", [eid])
    db.execute("DELETE FROM entries WHERE path = ?", [path])
    db.commit()


# ── Parsing ───────────────────────────────────────────────────────────────────

def extract_frontmatter(content: str) -> dict:
    """Extract key fields from YAML frontmatter."""
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm_text = m.group(1)
    result = {"raw": m.group(0)}

    # date
    dm = re.search(r"^date:\s*(.+)$", fm_text, re.MULTILINE)
    result["date"] = dm.group(1).strip() if dm else ""

    # tldr (block scalar or inline)
    tldr_m = re.search(r"^tldr:\s*\|?\n?(.*?)(?=\n\w|\Z)", fm_text, re.DOTALL | re.MULTILINE)
    if tldr_m:
        result["tldr"] = re.sub(r"\n\s+", " ", tldr_m.group(1)).strip()

    # topics
    topics_m = re.search(r"^topics:\s*\[(.+?)\]", fm_text, re.MULTILINE)
    if topics_m:
        result["topics"] = topics_m.group(1).strip()

    # decisions (YAML list)
    decisions_block = re.search(r"^decisions:\n((?:\s+-.*\n?)+)", fm_text, re.MULTILINE)
    if decisions_block:
        items = re.findall(r'^\s+-\s+"?(.+?)"?\s*$', decisions_block.group(1), re.MULTILINE)
        result["decisions"] = "; ".join(items)

    return result


def extract_decisions_section(content: str) -> str:
    """Extract ## Decisions Made body (stop at next ##)."""
    m = re.search(r"## Decisions Made\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


def chunks_for_log(path: Path, content: str) -> list[dict]:
    """Return chunks to index for a single session log."""
    fm = extract_frontmatter(content)
    if not fm:
        return []

    chunks = []

    # Chunk 1: frontmatter (dense signal)
    fm_text = fm["raw"]
    if len(fm_text) > 50:
        chunks.append({
            "chunk": fm_text,
            "type": "frontmatter",
            "date": fm.get("date", ""),
            "tldr": fm.get("tldr", ""),
            "topics": fm.get("topics", ""),
            "decisions": fm.get("decisions", ""),
        })

    # Chunk 2: decisions section body (if present and non-trivial)
    dec_body = extract_decisions_section(content)
    if len(dec_body) > 30:
        chunks.append({
            "chunk": f"Decisions from {path.stem}:\n{dec_body}",
            "type": "decisions",
            "date": fm.get("date", ""),
            "tldr": fm.get("tldr", ""),
            "topics": fm.get("topics", ""),
            "decisions": fm.get("decisions", ""),
        })

    return chunks


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(path_str: str):
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    db = open_db()
    # Remove stale entries for this path (re-indexing)
    delete_entries(db, str(path))

    content = path.read_text(encoding="utf-8")
    chunks = chunks_for_log(path, content)
    if not chunks:
        print(f"No indexable content in {path.name}")
        return

    indexed = 0
    for chunk in chunks:
        vec = embed(chunk["chunk"])
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) VALUES (?,?,?,?,?,?,?)",
            [str(path), chunk["date"], chunk["chunk"], chunk["type"],
             chunk["tldr"], chunk["topics"], chunk["decisions"]],
        )
        rowid = cur.lastrowid
        db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                   [rowid, serialize(vec)])
        indexed += 1

    db.commit()
    print(f"Indexed {indexed} chunk(s) from {path.name}")


def cmd_recent(n: int = 3, days: bool = False):
    """Return recent session frontmatters. Pure filesystem — no API calls.

    When days=False (legacy): return last N sessions, sorted by date then mtime.
    When days=True: return ALL sessions from the last N calendar days, sorted by
    date descending then mtime descending (newest first within each day).
    """
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    log_files = [f for f in VAULT_SESSION_LOGS.rglob("*.md") if ".obsidian" not in str(f)]

    # Parse date: prefer parent folder name (YYYY-MM-DD), fallback to frontmatter
    def get_date(p: Path) -> str:
        folder = p.parent.name
        if re.match(r"^\d{4}-\d{2}-\d{2}$", folder):
            return folder
        fm = extract_frontmatter(p.read_text(encoding="utf-8"))
        return fm.get("date", "0000-00-00")

    dated = [(get_date(f), f) for f in log_files]
    # Sort by date descending, then mtime descending (newest file first within same day)
    dated.sort(key=lambda x: (x[0], x[1].stat().st_mtime), reverse=True)

    if days:
        # Collect all unique dates, take the first N, return all sessions from those days
        seen_dates: list[str] = []
        for date, _ in dated:
            if date not in seen_dates:
                seen_dates.append(date)
            if len(seen_dates) > n:
                break
        target_dates = set(seen_dates[:n])
        selected = [(d, p) for d, p in dated if d in target_dates]
    else:
        selected = dated[:n]

    lines = ["## Recent Sessions"]
    for date, path in selected:
        content = path.read_text(encoding="utf-8")
        fm = extract_frontmatter(content)
        name = path.stem.replace("-", " ")
        tldr = (fm.get("tldr", "") or "").split(".")[0][:80]
        decisions = fm.get("decisions", "") or ""
        dec_part = f" | {decisions}" if decisions else ""
        lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
        lines.append(f"  (full log: {path})")

    print("\n".join(lines))


def cmd_learnings(since_days: int = 7, max_items: int = 3):
    """Surface recently strengthened or new high-confidence atoms since last /resume.

    Delta tracking: compares against ~/.deus/last_resume_learnings.txt to avoid
    showing the same learnings twice. Outputs nothing if no new learnings exist.
    """
    if not VAULT_ATOMS.exists():
        return

    from datetime import date as _date, timedelta
    today = _date.today()
    cutoff = today - timedelta(days=since_days)

    # Load previously shown learnings for delta tracking
    previously_shown: set[str] = set()
    if LAST_RESUME_LEARNINGS.exists():
        previously_shown = set(LAST_RESUME_LEARNINGS.read_text().strip().splitlines())

    # Scan all atoms
    candidates: list[dict] = []
    for atom_path in VAULT_ATOMS.glob("*.md"):
        content = atom_path.read_text(encoding="utf-8")
        fm = extract_frontmatter(content)
        if not fm:
            continue

        created_at = fm.get("date") or fm.get("raw", "")
        updated_at = ""
        corroborations = 1
        confidence = 0.5
        category = "fact"
        expired = False

        # Parse frontmatter fields from raw block
        raw = fm.get("raw", "")
        for line in raw.splitlines():
            if line.startswith("created_at:"):
                created_at = line.split(":", 1)[1].strip()
            elif line.startswith("updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
            elif line.startswith("corroborations:"):
                try:
                    corroborations = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("confidence:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("category:"):
                category = line.split(":", 1)[1].strip()
            elif line.startswith("ttl_days:"):
                ttl_str = line.split(":", 1)[1].strip()
                if ttl_str not in ("null", ""):
                    try:
                        ttl = int(ttl_str)
                        if created_at and (today - _date.fromisoformat(created_at)).days > ttl:
                            expired = True
                    except (ValueError, TypeError):
                        pass

        if expired:
            continue

        if not updated_at:
            updated_at = created_at

        try:
            update_date = _date.fromisoformat(updated_at)
        except (ValueError, TypeError):
            continue

        if update_date < cutoff:
            continue

        # Extract body text (after second ---)
        body = ""
        parts = content.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
        if not body:
            continue

        # Skip if already shown in a previous /resume
        if atom_path.name in previously_shown:
            continue

        # Classify: strengthened (updated > created, 2+ corroborations) vs new insight
        is_strengthened = (updated_at != created_at) and corroborations >= 2

        candidates.append({
            "path": atom_path,
            "name": atom_path.name,
            "body": body,
            "category": category,
            "corroborations": corroborations,
            "confidence": confidence,
            "is_strengthened": is_strengthened,
            "updated_at": updated_at,
        })

    if not candidates:
        return

    # Sort: strengthened patterns first, then by confidence desc, then recency
    candidates.sort(key=lambda x: (x["is_strengthened"], x["confidence"], x["updated_at"]), reverse=True)
    selected = candidates[:max_items]

    lines = ["## What's Emerging"]
    for item in selected:
        prefix = "Pattern confirmed" if item["is_strengthened"] else "New insight"
        suffix = f" (seen across {item['corroborations']} sessions)" if item["corroborations"] >= 2 else ""
        lines.append(f"- {prefix}: {item['body']}{suffix}")

    print("\n".join(lines))

    # Update delta tracking file
    LAST_RESUME_LEARNINGS.parent.mkdir(parents=True, exist_ok=True)
    shown_names = previously_shown | {item["name"] for item in selected}
    LAST_RESUME_LEARNINGS.write_text("\n".join(sorted(shown_names)) + "\n")


def cmd_query(query: str, top: int = 3, recency_boost: bool = False):
    db = open_db()

    # Check if anything is indexed
    count = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if count == 0:
        print("(index empty — run --rebuild first)", file=sys.stderr)
        sys.exit(1)

    has_atoms = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom'").fetchone()[0] > 0

    q_vec = embed(query)
    rows = db.execute(
        """
        SELECT e.path, e.date, e.tldr, e.topics, e.decisions, e.type,
               e.confidence, e.corroborations, v.distance
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        [serialize(q_vec), top * 3],  # 3x to have room for both atoms and sessions
    ).fetchall()

    # Partition into atoms and sessions; deduplicate sessions by path
    atom_results: list[dict] = []
    seen: dict[str, dict] = {}

    for path, date, tldr, topics, decisions, chunk_type, confidence, corroborations, dist in rows:
        if chunk_type == "atom":
            if has_atoms and len(atom_results) < top:
                atom_results.append({
                    "path": path, "chunk": tldr, "confidence": confidence or 0.0,
                    "corroborations": corroborations or 1,
                })
        else:
            if path not in seen or (chunk_type == "frontmatter" and seen[path]["type"] != "frontmatter"):
                seen[path] = {
                    "path": path, "date": date, "tldr": tldr,
                    "topics": topics, "decisions": decisions,
                    "type": chunk_type, "dist": dist,
                }
            # Without recency boost, stop early at top; with it, collect all candidates for re-ranking
            if not recency_boost and len(seen) >= top:
                break

    # Re-rank sessions by recency-adjusted distance
    if recency_boost and seen:
        from datetime import date as _date
        today = _date.today()
        for entry in seen.values():
            try:
                entry_date = _date.fromisoformat(entry["date"])
                age_days = (today - entry_date).days
                if age_days <= 7:
                    entry["dist"] -= RECENCY_BOOST_7D
                elif age_days <= 30:
                    entry["dist"] -= RECENCY_BOOST_30D
            except (ValueError, TypeError):
                pass
        # Re-sort by adjusted distance and keep top N
        ranked = sorted(seen.values(), key=lambda e: e["dist"])[:top]
        seen = {e["path"]: e for e in ranked}

    if not seen and not atom_results:
        sys.exit(1)  # trigger fallback in /resume

    lines = []

    # Known Facts — only atoms with ≥ 2 corroborations (well-established)
    high_conf = [a for a in atom_results if a["corroborations"] >= 2]
    if high_conf:
        lines.append("## Known Facts")
        for a in high_conf:
            cat = "fact"
            atom_p = Path(a["path"])
            if atom_p.exists():
                m = re.search(r"^category:\s*(\S+)", atom_p.read_text(), re.MULTILINE)
                if m:
                    cat = m.group(1)
            text = a["chunk"] or ""
            lines.append(f"- [{cat} | {a['confidence']:.2f}] {text} ({a['corroborations']}x)")
        lines.append("")

    if seen:
        lines.append("## Relevant Past Sessions")
        for entry in list(seen.values())[:top]:
            date = entry["date"] or "unknown date"
            name = Path(entry["path"]).stem.replace("-", " ")
            tldr = (entry["tldr"] or "").split(".")[0][:80]
            decisions = entry["decisions"] or ""
            dec_part = f" | {decisions}" if decisions else ""
            lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
            lines.append(f"  (full log: {entry['path']})")

    print("\n".join(lines))


def cmd_wander(seeds: list[str], steps: int = 3, top_k: int = 10):
    """
    Spreading activation over session log topics (no embeddings needed).
    Builds a topic co-occurrence graph from session logs, then spreads activation
    from seed topics to discover cross-domain connections.
    """
    from collections import defaultdict

    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    today = datetime.now().date()

    # Build weighted co-occurrence graph: edge_weight[t1][t2] += recency_weight
    edge_weight: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    topic_sessions: dict[str, list[str]] = defaultdict(list)

    log_files = sorted(VAULT_SESSION_LOGS.rglob("*.md"))
    log_files = [f for f in log_files if ".obsidian" not in str(f)]

    for log_file in log_files:
        fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
        if not fm.get("topics"):
            continue

        topics = [t.strip() for t in fm["topics"].split(",")]

        try:
            from datetime import date as _date
            session_date = _date.fromisoformat(fm.get("date", ""))
            age_days = (today - session_date).days
            recency = 2.0 if age_days <= 30 else 1.5 if age_days <= 90 else 1.0
        except (ValueError, TypeError):
            recency = 1.0

        for t in topics:
            topic_sessions[t].append(log_file.stem)

        for i, t1 in enumerate(topics):
            for t2 in topics[i + 1:]:
                edge_weight[t1][t2] += recency
                edge_weight[t2][t1] += recency

    if not edge_weight:
        print("No topics found in session logs.")
        return

    all_topics = set(edge_weight.keys())

    # Resolve seeds: exact then partial match
    resolved: list[str] = []
    for seed in seeds:
        s = seed.lower()
        if s in all_topics:
            resolved.append(s)
        else:
            resolved.extend(t for t in all_topics if s in t or t in s)

    # If no seeds provided or nothing matched, use topics from the 3 most recent sessions
    if not resolved:
        for log_file in sorted(log_files, reverse=True)[:3]:
            fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
            if fm.get("topics"):
                resolved.extend(t.strip() for t in fm["topics"].split(","))
        resolved = list(dict.fromkeys(resolved))[:5]

    if not resolved:
        print("Could not find any seed topics.")
        return

    # Spreading activation (decay=0.7, lateral inhibition to top_k per step)
    DECAY = 0.7
    activation: dict[str, float] = {t: 1.0 for t in resolved}
    all_visited: set[str] = set(resolved)

    for _ in range(steps):
        spread: dict[str, float] = {}
        for topic, strength in activation.items():
            neighbors = edge_weight.get(topic, {})
            total = sum(neighbors.values()) or 1.0
            for neighbor, weight in neighbors.items():
                if neighbor not in all_visited:
                    spread[neighbor] = spread.get(neighbor, 0.0) + strength * DECAY * (weight / total)
        if not spread:
            break
        top = sorted(spread.items(), key=lambda x: -x[1])[:top_k]
        activation = dict(top)
        all_visited.update(activation)

    print(f"## Wander: seeds = [{', '.join(resolved)}]\n")

    if activation:
        print(f"### Connected Topics\n")
        for topic, strength in sorted(activation.items(), key=lambda x: -x[1])[:top_k]:
            sessions = topic_sessions.get(topic, [])
            hint = f" ← {sessions[-1]}" if sessions else ""
            print(f"- **{topic}** ({strength:.2f}){hint}")

    # Collision candidates: activated topic pairs that don't directly co-occur
    # but share common neighbors (unexpected cross-domain bridges)
    activated_list = [t for t, _ in sorted(activation.items(), key=lambda x: -x[1])[:15]]
    collisions = []
    for i, t1 in enumerate(activated_list):
        for t2 in activated_list[i + 1:]:
            if t2 not in edge_weight.get(t1, {}):
                shared = set(edge_weight.get(t1, {}).keys()) & set(edge_weight.get(t2, {}).keys())
                if shared:
                    collisions.append((t1, t2, sorted(shared)[:3]))

    print(f"\n### Collision Candidates (indirect bridges)\n")
    if collisions:
        for t1, t2, via in collisions[:5]:
            print(f"- **{t1}** ↔ **{t2}**  (via: {', '.join(via)})")
    else:
        print("(none)")


def slugify(text: str) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "-".join(words[:5])


def _extract_content_for_llm(content: str, max_chars: int = 6000) -> str:
    """Return a token-efficient slice of a session log for the extraction LLM."""
    if len(content) <= max_chars:
        return content
    # Keep frontmatter + Decisions Made + Key Learnings only
    parts = []
    fm_m = re.match(r"^---\n.*?\n---", content, re.DOTALL)
    if fm_m:
        parts.append(fm_m.group(0))
    for section in ("## Decisions Made", "## Key Learnings"):
        sec_m = re.search(rf"{re.escape(section)}\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if sec_m:
            parts.append(f"{section}\n{sec_m.group(1).strip()}")
    trimmed = "\n\n".join(parts)
    return trimmed[:max_chars]


def extract_atoms(content: str) -> list[dict]:
    """Call Gemini Flash to extract 2-5 atomic facts from a session log."""
    prompt = (
        "You are an atomic fact extractor for a personal knowledge system.\n\n"
        "Given a session log, extract 2-5 atomic facts the user would want a future AI assistant to remember. "
        "Each fact must be:\n"
        "- A single sentence, timeless (not \"today we did X\" but \"prefers X over Y\")\n"
        "- About the USER's preferences, identity, or stable decisions — not about what happened\n"
        "- Actionable across future sessions\n\n"
        "Categories:\n"
        "- preference: user likes/dislikes, style choices (ttl: 365 days)\n"
        "- constraint: hard rules, always/never requirements (ttl: 365 days)\n"
        "- belief: opinions, worldview, tentative inferences (ttl: 90 days)\n"
        "- fact: identity info, context, environment details (no ttl)\n"
        "- decision: architectural or tool choices that affect future work (no ttl)\n\n"
        "Respond with ONLY a JSON array, no markdown fencing:\n"
        '[{"text": "...", "category": "preference|constraint|belief|fact|decision"}]\n\n'
        "If nothing is worth extracting (casual/social session with no stable decisions), respond with: []\n\n"
        f"SESSION LOG:\n{_extract_content_for_llm(content)}"
    )
    for model in GEN_MODELS:
        try:
            response = _client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=1024),
            )
            raw = response.text.strip()
            # Strip markdown fencing (opening and closing) if model adds it
            if raw.startswith("```"):
                raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
            atoms = json.loads(raw)
            return [a for a in atoms if isinstance(a, dict) and "text" in a and "category" in a]
        except json.JSONDecodeError:
            return []
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                print(f"  quota exhausted on {model}, trying fallback...", file=sys.stderr)
                continue  # try next model
            # Non-quota error (bad key, network, etc.) — no point retrying
            print(f"  WARN: extraction error ({model}): {e}", file=sys.stderr)
            return []
    print("  WARN: all generation models quota-exhausted, skipping extraction.", file=sys.stderr)
    return []


def find_duplicate_atom(db: sqlite3.Connection, vec: list[float]) -> int | None:
    """Return the entry id of an existing atom within DEDUP_L2_THRESHOLD, or None."""
    atom_count = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom'").fetchone()[0]
    if atom_count == 0:
        return None  # short-circuit: nothing to compare against
    row = db.execute(
        """
        SELECT e.id, v.distance
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE e.type = 'atom'
          AND v.embedding MATCH ?
          AND k = 1
        ORDER BY v.distance
        """,
        [serialize(vec)],
    ).fetchone()
    if row and row[1] <= DEDUP_L2_THRESHOLD:
        return row[0]
    return None


def bump_corroboration(db: sqlite3.Connection, entry_id: int):
    """Increment corroborations, recompute confidence, and update the atom .md file."""
    row = db.execute("SELECT path, corroborations FROM entries WHERE id = ?", [entry_id]).fetchone()
    if not row:
        return
    new_corr = row[1] + 1
    new_conf = min(0.5 + new_corr * 0.1, 0.95)
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "UPDATE entries SET corroborations = ?, confidence = ? WHERE id = ?",
        [new_corr, new_conf, entry_id],
    )
    atom_path = Path(row[0])
    if atom_path.exists():
        text = atom_path.read_text()
        text = re.sub(r"^confidence:.*$", f"confidence: {new_conf:.2f}", text, flags=re.MULTILINE)
        text = re.sub(r"^corroborations:.*$", f"corroborations: {new_corr}", text, flags=re.MULTILINE)
        text = re.sub(r"^updated_at:.*$", f"updated_at: {today}", text, flags=re.MULTILINE)
        atom_path.write_text(text)


def write_atom_file(atom: dict, source_path: str, today: str) -> Path:
    """Write an atom to the vault Atoms/ directory and return its path."""
    VAULT_ATOMS.mkdir(parents=True, exist_ok=True)
    cat = atom["category"]
    ttl_map = {"fact": None, "decision": None, "preference": 365, "constraint": 365, "belief": 90}
    ttl = ttl_map.get(cat, 365)
    ttl_line = f"ttl_days: {ttl}" if ttl is not None else "ttl_days: null"
    slug = slugify(atom["text"])
    path = VAULT_ATOMS / f"{cat}-{slug}.md"
    counter = 2
    while path.exists():
        path = VAULT_ATOMS / f"{cat}-{slug}-{counter}.md"
        counter += 1
    path.write_text(
        f"---\ntype: atom\ncategory: {cat}\ntags: []\n"
        f"confidence: 0.50\ncorroborations: 1\n"
        f"source: {source_path}\ncreated_at: {today}\nupdated_at: {today}\n{ttl_line}\n---\n"
        f"{atom['text']}\n"
    )
    return path


def cmd_extract(session_path: str):
    path = Path(session_path).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    content = path.read_text(encoding="utf-8")

    # Pre-flight: skip sessions with no stable decisions (saves 1 LLM call)
    has_decisions = bool(
        re.search(r"^decisions:\s*\n\s+-", content, re.MULTILINE)
        or re.search(r"## Decisions Made", content)
    )
    if not has_decisions:
        print("No decisions content — skipping extraction.")
        return

    atoms = extract_atoms(content)
    if not atoms:
        print("No atoms extracted.")
        return

    db = open_db()
    today = datetime.now().strftime("%Y-%m-%d")
    new_count, corroborated_count = 0, 0

    # Load existing atom texts for cheap text-equality dedup before embedding
    existing_texts = {
        r[0].strip().lower()
        for r in db.execute("SELECT chunk FROM entries WHERE type = 'atom'").fetchall()
    }

    for atom in atoms:
        text_lower = atom["text"].strip().lower()

        # 1. Text equality check — free, no API call
        if text_lower in existing_texts:
            row = db.execute(
                "SELECT id FROM entries WHERE type = 'atom' AND lower(chunk) = ? LIMIT 1",
                [text_lower],
            ).fetchone()
            if row:
                bump_corroboration(db, row[0])
            corroborated_count += 1
            print(f"  corroborated (text match): {atom['text'][:70]}")
            continue

        # 2. Embedding similarity check
        try:
            vec = embed(atom["text"])
        except Exception as e:
            print(f"  WARN: embed failed, skipping atom: {e}", file=sys.stderr)
            continue
        existing_id = find_duplicate_atom(db, vec)
        if existing_id:
            bump_corroboration(db, existing_id)
            corroborated_count += 1
            print(f"  corroborated: {atom['text'][:70]}")
        else:
            atom_path = write_atom_file(atom, str(path), today)
            cur = db.execute(
                "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations) "
                "VALUES (?, ?, ?, 'atom', ?, '', 0.50, 1)",
                [str(atom_path), today, atom["text"], atom["text"]],
            )
            db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                       [cur.lastrowid, serialize(vec)])
            new_count += 1
            print(f"  new atom: {atom['text'][:70]}")

    db.commit()
    print(f"Extracted {new_count + corroborated_count} atoms ({new_count} new, {corroborated_count} corroborated)")


def cmd_rebuild():
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    # Wipe and recreate DB
    if DB_PATH.exists():
        DB_PATH.unlink()
    db = open_db()
    db.close()

    log_files = sorted(VAULT_SESSION_LOGS.rglob("*.md"))
    log_files = [f for f in log_files if ".obsidian" not in str(f)]
    print(f"Found {len(log_files)} session logs. Indexing...")

    ok = 0
    for f in log_files:
        try:
            cmd_add(str(f))
            ok += 1
        except Exception as exc:
            print(f"  WARN: skipped {f.name}: {exc}", file=sys.stderr)

    # Re-index atoms (skip files already in DB with matching updated_at — mtime guard)
    atom_ok = 0
    if VAULT_ATOMS.exists():
        atom_files = sorted(VAULT_ATOMS.glob("*.md"))
        print(f"\nFound {len(atom_files)} atoms. Re-indexing...")
        db = open_db()
        for af in atom_files:
            try:
                content = af.read_text(encoding="utf-8")
                fm = extract_frontmatter(content)
                # Body = everything after the closing ---
                body = content[content.rfind("---") + 3:].strip()
                if not body:
                    continue
                # Mtime skip: if path + updated_at already in DB, skip embed call
                existing = db.execute(
                    "SELECT id FROM entries WHERE path = ? LIMIT 1", [str(af)]
                ).fetchone()
                if existing:
                    continue
                vec = embed(body)
                conf = float(fm.get("confidence", 0.5))
                corr = int(fm.get("corroborations", 1))
                date_str = fm.get("created_at", "")
                cur = db.execute(
                    "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations) "
                    "VALUES (?, ?, ?, 'atom', ?, ?, ?, ?)",
                    [str(af), date_str, body, body, fm.get("tags", ""), conf, corr],
                )
                db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                           [cur.lastrowid, serialize(vec)])
                atom_ok += 1
            except Exception as exc:
                print(f"  WARN: skipped atom {af.name}: {exc}", file=sys.stderr)
        db.commit()

    print(f"\nDone. {ok}/{len(log_files)} logs + {atom_ok} atoms indexed into {DB_PATH}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deus memory indexer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", metavar="PATH", help="Index a single session log")
    group.add_argument("--query", metavar="TEXT", help="Semantic search, returns top-K results")
    group.add_argument("--rebuild", action="store_true", help="Rebuild full index from scratch")
    group.add_argument(
        "--wander",
        nargs="*",
        metavar="TOPIC",
        help="Spreading activation from seed topics (no API key needed). "
             "E.g. --wander linear-algebra mechanics. Omit topics to seed from recent sessions.",
    )
    group.add_argument("--extract", metavar="PATH",
                       help="Extract atomic facts from a session log (uses Gemini Flash)")
    group.add_argument("--recent", type=int, metavar="N",
                       help="Return last N session frontmatters by date (no API call)")
    group.add_argument("--recent-days", type=int, metavar="N",
                       help="Return ALL sessions from the last N calendar days (no API call)")
    group.add_argument("--learnings", action="store_true",
                       help="Surface recently strengthened/new atoms since last /resume (no API call)")
    parser.add_argument("--top", type=int, default=3, help="Number of results for --query")
    parser.add_argument("--since", type=int, default=7,
                        help="Lookback window in days for --learnings (default: 7)")
    parser.add_argument("--steps", type=int, default=3, help="Activation steps for --wander")
    parser.add_argument("--recency-boost", action="store_true",
                        help="Boost recent results in --query (last 7d strong, 30d moderate)")
    args = parser.parse_args()

    global _client
    # Commands that need no API key
    if args.wander is not None:
        cmd_wander(args.wander or [], steps=args.steps, top_k=args.top or 10)
        return
    if args.recent is not None:
        cmd_recent(args.recent)
        return
    if args.recent_days is not None:
        cmd_recent(args.recent_days, days=True)
        return
    if args.learnings:
        cmd_learnings(since_days=args.since, max_items=args.top)
        return

    _client = genai.Client(api_key=load_api_key())

    if args.add:
        cmd_add(args.add)
    elif args.query:
        cmd_query(args.query, top=args.top, recency_boost=args.recency_boost)
    elif args.rebuild:
        cmd_rebuild()
    elif args.extract:
        cmd_extract(args.extract)


if __name__ == "__main__":
    main()
