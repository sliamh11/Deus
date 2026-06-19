---
name: learn-this
description: Teach the user a topic and persist what they learn as queryable vault memory, grounded in NotebookLM. Vault-native companion to /teach.
user_invocable: true
argument-hint: "What do you want to learn?"
---

# /learn-this

Teach the user something and make the learning **stick across sessions** by persisting it into the
semantic memory vault, so the next session (and any project) can recall what they already know.

This is the **vault-native** sibling of `/teach`. Where `/teach` builds a directory-based,
multi-week course workspace (mission file, lesson HTML, local learning-records), `/learn-this`
keeps no local workspace: it writes each learning as a **vault memory node** retrievable anywhere
via `memory_tree.py`, and grounds knowledge in **NotebookLM** rather than parametric guessing.

## Relationship to /teach (read this — single source of truth)

- `/teach` → a self-contained course workspace in the current directory. Best for a sustained,
  structured curriculum with rich interactive HTML lessons.
- `/learn-this` → lightweight, cross-session learning capture into the global vault + NotebookLM.
  Best for "teach me X and remember that I learned it" across projects.

**Never duplicate a learning record across both.** A `/teach` workspace owns its local
`learning-records/`; `/learn-this` owns `$VAULT/Learning/`. If a learning relates to a `/teach`
course, link it with a `see_also` edge — do not copy it.

## Steps

### 1. Resolve the vault path

Read `~/.config/deus/config.json` and use its `vault_path`. If `DEUS_VAULT_PATH` is set, use that
instead. All paths below use `$VAULT` for this resolved path. (Same resolution as `/checkpoint`,
`/compress`, `/preserve` — keep it portable; never hardcode a personal path.)

### 2. Establish the mission

Understand **why** the user wants to learn this — the concrete real-world goal it serves. This
grounds every teaching choice. If it is unclear, ask before teaching; a vague mission produces
abstract, ungrounded lessons. The mission is captured inside the first learning node (step 6), not
in a separate file.

### 3. Compute the zone of proximal development — from the vault

Query the vault for what the user already knows on this topic:

```bash
python3 ~/deus/scripts/memory_tree.py query "<topic> — what has the user already learned?"
```

Read the returned learning nodes. Teach the **next** thing that builds on them — challenging "just
enough", neither re-teaching the known nor jumping past a gap. This is the cross-session ZPD (vs
`/teach`, which reads a local `learning-records/` folder).

### 4. Ground the knowledge in NotebookLM

Do not trust parametric knowledge. Pull high-trust material from NotebookLM:

- Pick a relevant notebook (the user's course notebooks are listed in the vault's `STUDY.md`).
  `mcp__notebooklm-mcp__notebook_query` for explanations, definitions, and citations.
- For a long-form lesson, `mcp__notebooklm-mcp__studio_create` (audio / video / slides), poll
  `mcp__notebooklm-mcp__studio_status`, and link the artifact in the learning node.

**Auth degradation is explicit, never silent.** If a NotebookLM call fails authentication, tell the
user to run `nlm login` in their terminal, and ask whether to proceed *without* NotebookLM grounding
(flagging that the lesson will then rest on lower-trust sources). Do not silently skip grounding.

### 5. Deliver the lesson

Teach in the user's style (see the `ai-role` and study preferences in the vault):

- **Visual-first** — diagrams / flowcharts before walls of text (Mermaid where useful).
- **Feynman-first** — simple analogy → why it matters → how it connects to what they already know.
- Keep it short and within working memory; one tangible win per lesson, tied to the mission.
- Cite the NotebookLM sources so claims are checkable.
- Close with a **tight retrieval-practice loop**: ask the user to recall/apply it and give immediate
  feedback. Effortful retrieval is what builds long-term retention (storage strength), not fluency.

### 6. Persist a learning record as a vault node

**Only when there is real evidence of understanding** (a correct recall, a solved problem, a
corrected misconception) — coverage is not learning. Write:

```
$VAULT/Learning/<topic-slug>/NNNN-<dash-case-name>.md
```

`NNNN` = next integer in that topic folder (scan for the highest existing). Use the frontmatter and
guidance in [LEARNING-NODE-FORMAT.md](./LEARNING-NODE-FORMAT.md). **Write an `id` in the
frontmatter** — the memory tree indexer and the auto-embed hook *skip* any node without one (they
return `no_id`), so an id-less node is silently never retrievable. Mint one in the `make_id` format
(32-char hex = 48-bit ms timestamp + 80-bit random):

```bash
python3 -c "import time,secrets; print((int(time.time()*1000).to_bytes(6,'big')+secrets.token_bytes(10)).hex())"
```

The `description` field is the embedding source — make it a tight summary of *what was learned and
why it matters*, since that is what future `memory_tree.py query` calls match on. Add `see_also`
edges to related learning nodes, the vault `STUDY.md`, and any related `/teach` workspace.

### 7. Make it retrievable

Writing the node file triggers the vault PostToolUse hook (`memory_tree_hook.py`), which embeds the
node into the **memory tree** — so the `memory_tree.py query` in step 3 finds it in future sessions
automatically. If you need it indexed immediately, or the hook is not active (e.g. a headless run),
run:

```bash
python3 ~/deus/scripts/memory_tree.py build
```

(walks the vault and upserts new nodes + edges). **Do not use `memory_indexer.py --add`** — that
indexes *session logs* into a separate atom/search database, not the navigation tree that step 3
queries; a learning node added there would not be reachable via `memory_tree.py query`.

### 8. Wisdom — point to a community

When a question needs real-world practice beyond what you can give, point the user to a
high-reputation community (forum, subreddit, local group) where they can test the skill. Respect a
stated preference not to join communities.

## Notes

- The mission may shift as the user learns; when it does, write a new learning node capturing the
  shift rather than rewriting history (vault is soft-append; never overwrite a node's meaning).
- Keep lessons grounded in the mission — abstract lessons with no real-world anchor do not stick.
