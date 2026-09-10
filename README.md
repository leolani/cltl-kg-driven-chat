# kg-chat

Turns a coaching-style chat conversation (a diabetes patient and a lifestyle coach) into a
knowledge graph, and closes the loop by having the graph itself drive part of the conversation:
when a new turn adds an activity the graph doesn't know much about yet, the KG's own gaps are
turned into the coach's next follow-up question.

```
 human turn ──► semantic-role extraction ──► push to knowledge graph
                       (LLM)                     (GraphDB / SPARQL)
                                                        │
                                                        ▼
 agent turn  ◄── reply generation ◄── gap found? ── gap finder (SPARQL)
              (LLM, KG-grounded        │
               question OR             │ no gap / no new triples
               generic LLM reply) ◄────┘
                    │
                    ▼ (if the question was a yes/no agent-confirmation)
        human confirms / denies / denies+corrects
                    │
                    ▼
      inferred triple pushed straight to the KG
```

Gaps for one activity are asked about **one at a time, in sequence**, and a gap is never asked
about twice. A gap on *who* did an activity is treated specially: since this is a single-human
chat, the human is assumed to be the agent, so instead of an open "who did this?" question the
agent asks for **confirmation** ("Just to confirm, was that you...?") — a confirm/deny/deny-with-
correction reply is turned straight back into a KG triple instead of prompting yet another
question.

Driven from two short notebooks under `notebooks/` — **[`chat_session.ipynb`](notebooks/chat_session.ipynb)**
(plain chat) and **[`kg_chat_session.ipynb`](notebooks/kg_chat_session.ipynb)** (KG-populating
chat) — both importing `ChatSession`/`KgChatSession` from **[`notebooks/chat_sessions.py`](notebooks/chat_sessions.py)**,
which in turn wires together the two independent pipelines living under `src/cltl/`. Both
notebooks drive their live chat through **[`notebooks/kg_chat_gui.py`](notebooks/kg_chat_gui.py)**:
a single Tkinter window showing the whole transcript, with the human typing into that same
window instead of the notebook's own `input()` prompt.

## Contents

- [Quick start](#quick-start)
- [Pipeline overview](#pipeline-overview)
- [The turn schema](#the-turn-schema)
- [`src/cltl/events_from_chat/` — chat → semantic roles → knowledge graph](#srclcltleventsfromchat--chat--semantic-roles--knowledge-graph)
- [`src/cltl/chat_from_kg/` — knowledge graph → gap → reply](#srclcltlchatfromkg--knowledge-graph--gap--reply)
- [`notebooks/` — putting it together](#notebooks--putting-it-together)
- [Setup](#setup)
- [Known rough edges](#known-rough-edges)

## Quick start

1. Install `requirements.txt` into a virtualenv (`.venv` is already set up in this repo).
2. Have a GraphDB (or other SPARQL 1.1) repository running and reachable, e.g.
   `http://localhost:7200/repositories/event_sandbox` — only needed for `kg_chat_session.ipynb`.
3. `export OPENAI_API_KEY=...` (required — several modules read it at *import* time, not just
   when a client is constructed).
4. Open `notebooks/chat_session.ipynb` for a plain chat, or `notebooks/kg_chat_session.ipynb` for
   the KG-populating version, and run the cells top to bottom. `chat_session.ipynb` only needs
   OpenAI, or nothing at all if you use the built-in `mock_agent`; `kg_chat_session.ipynb` needs
   both OpenAI and the knowledge graph. The live-chat cell in each opens its own window (needs a
   display and Tkinter — see [`notebooks/kg_chat_gui.py`](notebooks/kg_chat_gui.py)); type there,
   or type "bye" (or click **Quit**) to end the conversation and let the cell finish. Quitting
   writes the transcript and a statistics summary to two timestamped JSON files next to the
   notebook and prints their paths.

## Pipeline overview

There are two independent, reusable pipelines, plus the notebook that chains them per turn:

| Pipeline | Direction | Lives in | Turns a **conversation turn** into... |
|---|---|---|---|
| SRL extraction → KG population | chat → graph | `events_from_chat/` | ...RDF triples describing the activity/condition it mentions (who, what, when, where, ...). |
| Gap finding → reply generation | graph → chat | `chat_from_kg/` | ...(the KG's state around that activity into) a natural-language follow-up question about whatever the graph doesn't know yet. |

`KgChatSession` (in `notebooks/chat_sessions.py`) runs the first pipeline on every human turn, and
— only when that turn actually added something new — runs the second pipeline to decide what the
agent says next, falling back to a generic LLM reply otherwise.

## The turn schema

Every pipeline stage speaks the same flat turn dict:

```python
{
    "chat": <chat identifier>,      # which conversation this turn belongs to
    "human": <human's name>,        # the patient's name, same for every turn in the chat
    "date": <date the chat took place>,   # "%Y,%b,%d", e.g. "2013,Jan,31"
    "turn": <turn identifier>,      # 1, 2, 3, ... in speaking order within this chat
    "speaker": <human's name or "agent">,
    "utterance": <the text that was said>,
}
```

`ChatSession` (in `notebooks/chat_sessions.py`) produces lists of these. `LLM_EventExtraction` (in
`events_from_chat`) consumes them one at a time and returns **annotation entries**:

```python
{"chat": ..., "date": ..., "human": ..., "Input": <turn dict above>, "Output": [<SRLAnnotation>, ...]}
```

`populate_ekg_from_annotations` consumes lists of *lists* of these annotation entries (one inner
list per conversation) and pushes them into the graph.

## `src/cltl/events_from_chat/` — chat → semantic roles → knowledge graph

| File | Role |
|---|---|
| `data_type.py` | Closed vocabularies (`ActivityType`, `RoleType`, `EmotionLabel`, `Factuality`, `Certainty`, `TemporalType`, ...) shared by the prompt, the Pydantic models, and the annotation tool's dropdowns. |
| `prompts.py` | The system prompt (`prompt_conversational_srl_annotation`) instructing the LLM to extract one turn's activities/conditions at a time, using prior turns only as context. |
| `llm_event_triples_openai_pydantic.py` | `LLM_EventExtraction`: calls an OpenAI model with `data_type`'s vocabularies as a structured-output tool schema (`SRLAnnotations`/`SRLAnnotation` Pydantic models), and validates/auto-corrects the result (`check_compliance`, offset recovery). Its `timeout=` constructor parameter (default `DEFAULT_TIMEOUT`, 60s) and `max_retries=0` bound how long a stuck/slow OpenAI call can block — see [Timeouts](#timeouts). |
| `events_to_capsules.py` | Converts one annotation entry's `Output` (a list of `SRLAnnotation`s) into `cltl.brain` "capsules" — one per activity/condition, with RDF triples for every semantic role, keyed by the activity's own `activity_id` so the same real-world activity always maps to the same graph subject. A role filler that's exactly a first-/second-person pronoun (`I`/`me`/`mine`/`myself`, `you`/`yours`/`yourself`) is resolved to the identity it refers to — the turn's own speaker for first person, the other party in the two-party conversation for second person — and linked with that identity's own `http://cltl.nl/leolani/friends/<name>` URI (`_pronoun_identity()`/`_role_filler_object()`), instead of the bare pronoun ending up as an unlinked literal in the graph. |
| `populate_ekg.py` | `populate_ekg_from_annotations`: opens a `cltl.brain.LongTermMemory` (SPARQL-backed) and pushes the derived capsules into it. |
| `perspective/` | `GoEmotionDetector` (a `transformers` pipeline) and Ekman/GoEmotion label mappings — used by an older, un-annotated-activity variant of the pipeline (`get_scenarios_from_srl_annotations_identifying_activities_in_context_and_inferring_perspectives`); not needed for the activity-id-based path the notebook uses. |

### `LLM_EventExtraction`: batch vs. incremental

- **`annotate_all_turns_in_conversation(input)`** — the original, *batch* entry point: given
  `{"chat", "human", "date", "turns": [...]}`, it resets its own state and re-annotates every
  turn from scratch, in order. Good for annotating a conversation you already have in full (see
  `__main__` at the bottom of the file).
- **`annotate_new_turn(chat, human, date, turn)`** / **`reset_conversation()`** — the
  *incremental* sibling added for the live-chat use case: it keeps `self._history` /
  `self._known_activities` alive **across calls**, so each new turn costs exactly one OpenAI call
  while the model still sees every real prior turn as context (so cross-turn activity
  coreference — reusing the same `activity_id` for "it"/"that" — still works). One instance holds
  the state for exactly one conversation; call `reset_conversation()` before reusing an instance
  for a different one. This is what `KgChatSession` uses.

Each activity/condition gets an `activity_id` like `"chat254.1"` (chat number + a per-conversation
counter), reused every time the same real-world activity is mentioned again — that id is what
`events_to_capsules.py` turns into the RDF subject URI (`http://cltl.nl/leolani/n2mu/chat254.1`).

## `src/cltl/chat_from_kg/` — knowledge graph → gap → reply

| File | Role |
|---|---|
| `kg_gap_finder.py` | Queries a live SPARQL endpoint or local RDF file for five kinds of "knowledge gaps" (untyped entities, missing predicates, dangling references, missing predicate-object pairs at the type level and at the instance level), in the spirit of the Leolani Brain's own curiosity thoughts. `analyze(...)` is the notebook-friendly entry point (`endpoint=`, `subject_uri=`, `threshold=`). The three predicate-based gap kinds (missing predicates, and missing predicate-object pairs at the type/instance level) only consider `data_type.SemanticRole` predicates by default (`semantic_role_predicates()`) — so a provenance/bookkeeping predicate like `gaf:denotedIn`/`denotedBy` is never reported as a "gap"; pass `predicate_filter=None` (or `--all-predicates` on the CLI) to consider every predicate instead. Each of those three gap kinds also attaches `peer_examples` to every row: the actual values peers *do* have for the missing predicate, most-frequent first (e.g. a missing `time` gap might carry `[{"value": "in the morning", "count": 3}, ...]`) — capped by `peer_example_limit` (`--peer-examples` on the CLI, default 5, 0 disables). |
| `llm_triple_replier.py` | `LLMTripleReplier`: turns a structured "thing to say" (a gap, or a `cltl.brain` thought) into natural language via an LLM. Supports two backends — `backend="ollama"` (a local Ollama server, e.g. `llama3.2`) or `backend="openai"` (same `OPENAI_API_KEY` as everything else); `langchain_ollama` is only imported when the Ollama backend is actually selected. Its `timeout=`/`max_retries=0` (`backend="openai"` only, default `DEFAULT_TIMEOUT`, 60s) bound how long a stuck/slow OpenAI call can block `reply()` — see [Timeouts](#timeouts). |
| `prompts/instruct.py` | `Instruct`: canned system prompts telling the LLM how to paraphrase a given input into one short, specific sentence — a statement, an answer, a subject/object gap (with or without peer examples woven in), an agent-confirmation question, a classification of the human's reply to one (`CONFIRM`/`DENY`/`CORRECT: <value>`), a brief acknowledgement once a gap is filled, a novelty, a conflict. |
| `prompts/response_processor.py` | `PromptProcessor`: builds the actual `[instruct, content]` prompt for a given piece of structured input. `get_all_prompt_input_from_response(response)` does this for `cltl.brain`'s own "thought" schema (statement novelty, negation conflicts, subject/complement gaps — see `data/thoughts-responses.json`); `get_prompt_for_kg_gap(gap, kind, human=...)` does the equivalent for a `kg_gap_finder.py` gap row (see below), plus `get_prompt_for_confirmation_response(question, reply)` and `get_prompt_for_gap_filled_ack(gap, value)` for the confirmation round-trip. |

### Fast peer selection for a single subject

`find_predicate_gaps()`/`find_predicate_object_gaps()`/`find_predicate_object_instances_gaps()`
compute a gap by comparing one subject against its "peers" (other instances of the same kind).
Given only a `subject_filter` (no `class_uri`) — exactly how `KgChatSession` always calls
`analyze()` — they used to still scan every instance of *every* `rdf:type` class in the whole
graph to do that, then throw almost all of it away, keeping only the one row for the subject:
correct, but far too slow to do on every turn against a graph of any real size (measured on this
project's own `event_sandbox`: ~4600 SPARQL queries / ~8s for the full scan vs. ~40 queries /
~0.3s with the fix below, and the gap widens with graph size since the old cost scales with the
*whole graph*, not the subject's own peer group).

`find_comparison_peers(graph, subject_uri)` replaces that scan with a small, targeted peer group,
tried in order (within instances sharing any of the subject's own non-generic `rdf:type`s —
`GENERIC_TYPES` like `owl:Thing`/`gaf:Instance`, which nearly every resource has, are excluded so
they can't defeat the narrowing):

1. **Same or similar `rdfs:label`** (`_labels_similar()`: exact match, substring, or a shared
   word longer than 3 characters) — almost certainly the same real-world kind of activity (two
   mentions of "cycling").
2. **If that leaves fewer than 2 peers:** same type *and* the same value for an
   `agent`/`agent_patient` role (`AGENT_ROLE_PREDICATES`) — e.g. the same person did it.
3. **If still fewer than 2:** same type only (the original comparison basis, but scoped to just
   the subject's own type(s), never the whole graph).

Each stage's data (labels / types / agent values for the whole candidate pool) is fetched in one
SPARQL query via a `VALUES` clause, not one query per candidate instance. This path only kicks in
for `subject_filter` given *without* `class_uri`; pass both if you want the exhaustive "every
instance of exactly this class" comparison for a known subject instead.

**Every SPARQL query `kg_gap_finder.py` issues is logged to stdout as it runs** (`run_query()`):
row count and elapsed wall-clock time, e.g. `[kg_gap_finder] query #6: 32 row(s) in 0.003s -- SELECT
?instance ?agentValue WHERE { VALUES ?t { ... } ... }` (the query text is collapsed to one line and
truncated). `build_report()` (so `analyze()` and the CLI too) resets the count at the start of each
report and prints a `[kg_gap_finder] N queries, T.Ts total` summary at the end. Set
`kg_gap_finder.LOG_QUERIES = False` for quiet runs.

### From a gap row to a question

`kg_gap_finder`'s gap rows (`class`, `predicate`, `subject`, optionally `object_type`/`object`,
`subject_triples`, `peer_examples`, ...) don't look anything like `cltl.brain`'s thought objects
(`_known_entity`, `_entity`, `_types`, ...), so `get_prompt_for_kg_gap` is a separate adapter, not
a reinterpretation of `get_all_prompt_input_from_response`. It:

1. Labels the subject from its own `rdfs:label` (already present in `subject_triples`), skipping
   any label that's just the subject's own `activity_id` — a subject can carry more than one
   label, since a later turn that refers back to an activity without repeating a phrase
   (`events_to_capsules.get_triples_with_types_and_activity_id`) falls back to asserting the
   `activity_id` itself as the label — and falling back to a human-ish rendering of its URI
   (`local_name()`: last path segment, camelCase and `_` split into words, lowercased —
   `.../time/recurringTime` → `"recurring time"`) only if no real label exists at all.
2. If the gap's predicate is `agent`/`agent_patient` (`AGENT_PREDICATES`) and a `human` name was
   given, delegates to `get_prompt_for_agent_gap(gap, human)` instead — see below — and stops
   here; everything past this point is the non-agent path.
3. Builds a `"<subject>, <predicate>, <gap type>"` text (`gap type` is `"something"` for a bare
   missing predicate, or the missing object's type/value for the two finer-grained gap kinds),
   appending `peer_examples` as `". Examples from similar peers: <value> (<Nx>), ..."` when the
   row has any.
4. Wraps it with `Instruct.get_instruct_for_subject_gap()` (or
   `get_instruct_for_subject_gap_with_examples()` when peer examples were appended) — phrase this
   as a who/where/when/what follow-up question, weaving in 1-2 examples as suggestions when
   present — and hands it to `LLMTripleReplier.reply(...)`.

### Agent gaps: confirmation instead of an open question

Since each chat is one human talking with the agent, a missing agent-like role almost always
just means "you" — so `get_prompt_for_kg_gap(gap, kind, human=...)` asks the human to **confirm**
that instead of asking an open "who did this?" (`get_prompt_for_agent_gap` /
`Instruct.get_instruct_for_agent_confirmation()`, e.g. *"Just to confirm, was that you who went
for a run?"*).

**"Agent-like" is a group, not four independent predicates.** `agent`, `agent_patient`,
`participant` and `experiencer` are interchangeable alternatives for the same underlying fact —
`data_type.SemanticRole`'s own docstring documents `agent_patient`/`experiencer` as replacing
`agent` (+ `patient`) when a single participant already covers that role, and `participant` as
the catch-all for anything none of the others fit — never meant to be filled in side by side for
one activity. `kg_gap_finder.py`'s `PREDICATE_GROUPS`/`_canonicalize_predicate()` collapse all
four to one canonical `agent` predicate everywhere gaps B/D/E count or compare predicates, so an
instance that already has, say, `experiencer` filled is never separately flagged by B as missing
`agent` — the group is treated as one fact. `prompts.response_processor.AGENT_PREDICATES` and
`events_to_capsules.AGENT_LIKE_ROLES` mirror the same four-role set (kept in sync by hand, same
convention as `semantic_role_predicates()`/`NAMESPACE` elsewhere in this pair of modules) so the
confirmation-question dispatch and the extraction pipeline's own agent-defaulting
(`add_speaker_as_agent()`) agree on exactly which roles count as "agent-like."

**B only, not D/E, for this group.** D and E additionally ask "does this instance's value for a
predicate match the *specific* value/type most peers share" — meaningful for e.g. location or
time (peers doing the same kind of activity plausibly cluster around a shared place or hour), but
not for an agent-like role: who performed/experienced one activity is that instance's own
business, with no sensible "peers mostly share this exact same agent" expectation to compare
against. Without an exclusion, an instance whose agent-like value simply *differs* from whatever
happens to be most common among its peers (e.g. most peers' `agent` is "the doctor", this one's
`experiencer` is the human) would still get a spurious D/E gap on the canonical `agent` predicate
— which reaches the exact same confirmation-question dispatch as a B gap, asking "was that you?"
even though the instance already has an agent-like role filled. `D_E_EXCLUDED_PREDICATES`
subtracts the whole agent-like group from D's and E's default `predicate_filter` (B's is
untouched), so only "is *any* agent-like predicate filled at all" is ever asked about — never
"does your specific agent value match peers'."

`KgChatSession` (see below) then routes the human's *next* turn through
`get_prompt_for_confirmation_response(question, reply)` — a classifier prompt
(`Instruct.get_instruct_for_confirmation_response()`) that reduces any natural-language reply to
exactly one of `CONFIRM` / `DENY` / `CORRECT: <value>` — and:

- **confirm** → pushes `(subject, predicate, human)` to the KG directly (bypassing the general
  SRL extractor, which can't make sense of a bare "yes") and acknowledges via
  `get_prompt_for_gap_filled_ack(gap, human)`.
- **deny + correction in the same reply** (e.g. *"No, that was my son"*) → pushes
  `(subject, predicate, <correction>)` instead, and acknowledges the same way.
- **deny only** → pushes nothing; falls back to the plain open question for the same gap
  (`get_prompt_for_kg_gap(gap, kind)`, no `human=` this time), so the human can just answer it.

Any classifier output that isn't cleanly one of the three forms is treated as a plain `DENY` —
the safe default, since it never pushes an unverified triple.

## `notebooks/` — putting it together

`notebooks/chat_sessions.py` is a plain Python module (not a notebook) holding the two classes
and their helpers; both notebooks just `from chat_sessions import ...` and drive them. Splitting
it out means `chat_session.ipynb` never pays for `KgChatSession`'s heavier dependencies —
`_load_kg_dependencies()` (see below) only runs the first time a `KgChatSession` is actually
constructed, not on import.

| File | Defines |
|---|---|
| `chat_sessions.py` — setup | `_load_key()`, `_openai_client()`, `_today()`, `default_system_prompt(human)`, `openai_agent(model)` — an `agent_fn(messages) -> str` backed by OpenAI chat completions. |
| `chat_sessions.py` — `ChatSession` | One conversation. `say(utterance)` records a human turn, gets one agent turn in reply via `agent_fn`, appends both as flat turn dicts. `run_interactive()` drives that from `input()` in a loop. `as_conversation()` returns the nested `{chat, human, date, turns: [...]}` shape `annotate_all_turns_in_conversation` expects. |
| `chat_sessions.py` — demo helpers | `mock_agent(messages)` (no API key needed) and `simulate_chat(...)`, for exercising the turn format without typing anything; `save_turns(turns, path)` to write JSON. |
| `chat_sessions.py` — `KgChatSession` | The full KG loop (see below). |
| `kg_chat_gui.py` | `run_gui(session)`: opens one Tkinter window (`ChatWindow`) for a `ChatSession`/`KgChatSession` — the whole transcript scrolls on the left, and the human types into an entry box built into the *same* window (no `input()`, no popup). Each agent turn is tagged **[KG]**/**[LLM]** from `session.reply_sources` (a plain `ChatSession` has none, so it's always **[LLM]**). For a `KgChatSession`, a **Gap sensitivity** slider adjusts `session.gap_threshold` live, mid-conversation — dragging it also drops any already-cached per-instance gap queues, so the new sensitivity takes effect on the very next gap lookup rather than only once whatever was already queued happens to drain (`_on_gap_threshold_change`); not shown for a plain `ChatSession`, which has no `gap_threshold`. A **Text size** slider (always shown, 16pt default) live-resizes the whole conversation area at once via shared `tkinter.font.Font` objects. When `session.kg_address` is a GraphDB repository, the window splits and a live **graph panel** appears on the right (see below), with its own independent **Font size** slider. Talking to the graph/LLM runs on a background thread per turn so the window never freezes; the entry box is never disabled, so a quit word ("bye" etc., checked first in `_on_send`) or the always-enabled **Quit** button closes the window immediately even if a reply is stuck (`_on_close` calls `root.quit()` before `root.destroy()` — destroy alone doesn't reliably end a `mainloop()` that Jupyter is driving, which left the window up and the cell hanging). Quitting also writes the transcript and a statistics summary to timestamped JSON files via `save_session()`/`session_statistics()` — plus, for a `KgChatSession`, its per-turn `turn_log` (see below) as a third file — (`save_dir=None` disables). Returns `session.turns`, so it's a drop-in replacement for `run_interactive()` in a notebook cell. |
| `chat_session.ipynb` | Imports `ChatSession`/`mock_agent`/`simulate_chat`/`save_turns`/`run_gui` and runs a plain chat: live (`run_gui(ChatSession(...))`) or scripted (`simulate_chat()`), then inspects/saves the resulting turns. |
| `kg_chat_session.ipynb` | Imports `KgChatSession`/`run_gui`, sets `KG_ADDRESS`/`KG_LOG_DIR`, and runs a live KG-populating chat, then inspects `annotations`/`kg_pushes`/`reply_sources`. |

### `KgChatSession(ChatSession)`

For every new **human** turn, `say()` branches on whether the *previous* agent turn was an
agent-confirmation question (`self._pending_confirmation`, armed by `_reply_from_gaps` — see
[Agent gaps: confirmation instead of an open question](#agent-gaps-confirmation-instead-of-an-open-question)
above):

- **A confirmation is pending** — the turn is routed to `_handle_confirmation_reply()` instead of
  the general SRL pipeline: it classifies the reply, then either pushes the confirmed/corrected
  triple straight to the KG (`_push_gap_triple`, bypassing `LLM_EventExtraction` entirely — it
  isn't built to make sense of a bare "yes") or, on a plain denial, falls back to the open
  question for the same gap.
- **Otherwise**, the normal flow:
  1. Immediately runs `LLM_EventExtraction.annotate_new_turn(...)` on the turn and, if it
     produced anything, pushes it into the graph with `populate_ekg_from_annotations(...)`
     (`_annotate_and_push`, returning the KG subject URI(s) of whatever activity/condition was
     just asserted).
  2. **If new triples were pushed**, `_reply_from_gaps` asks `_next_gap` for the next gap to ask
     about around those subject URIs and, via `LLMTripleReplier`, turns it into the agent's
     reply — arming `self._pending_confirmation` if that gap was an agent-confirmation.
  3. **Otherwise** (no new triples, or no gap left to ask) falls back to the default `agent_fn`
     reply, same as plain `ChatSession`.

Either way, the agent's reply is then recorded as its own turn and annotated + pushed too (so the
extractor's running context includes what the agent said, same as the batch pipeline would) —
except a confirmation-reply turn's *human* turn skips that generic annotation, for the same
reason it skipped it going in.

**Gaps are asked one at a time, in sequence, and never repeated.** `_next_gap(subject_uris)`
keeps a per-activity-instance queue (`self._gap_queues`, most-affected gap first) fetched by one
`kg_gap_finder.analyze()` call; it drains that queue fully — one gap per turn — before launching
a fresh query for the same instance again, and every gap it hands out is recorded in
`self._asked_gap_keys` (keyed by `(subject, predicate)`, so a gap resurfacing at a different
gap-kind or via a different instance's queue is still skipped).

`self.annotations`, `self.kg_pushes` and `self.reply_sources` (`"gap"`, `"default"` or
`"timeout"` per agent turn) accumulate for inspection after a session.

### `self.turn_log` — a per-turn diagnostic log

One entry per turn (both the human's and the agent's, same indexing as `self.turns`), each a
`{"turn", "speaker", "utterance", "triples_pushed", "gap_queries", "selected_gap"}` dict:

- **`triples_pushed`** — `[{"subject", "predicate", "object"}, ...]`, a simplified view of what
  *this* turn told the knowledge graph (`_extraction_log_triples()`). Not the exact RDF (see
  `events_to_capsules.py` for that — a role filler's raw phrase is shown as-is here, not resolved
  to a pronoun's identity or linked via a real URI) — close enough to see at a glance what was
  asserted, without duplicating that module's triple-building logic just to log it.
- **`gap_queries`** — one `{"subject", "threshold", "found", "after_dedup"}` entry per *fresh*
  `kg_gap_finder` query actually run while producing this turn's reply (`"found"` breaks down the
  raw B/D/E counts before dedup). Empty when no fresh query happened — e.g. an already-queued gap
  was used instead (see `_next_gap()`'s queue-draining above), or this is the agent's own turn,
  which never triggers gap-finding at all.
- **`selected_gap`** — `{"subject", "predicate", "kind", "peer_coverage"}`, the gap this turn's
  reply was actually about: freshly found this turn, or — for a turn that answers a pending
  agent-confirmation — the one being *resolved* (found on an earlier turn; see [Agent gaps: confirmation
  instead of an open question](#agent-gaps-confirmation-instead-of-an-open-question)). `None` if
  the reply wasn't gap-driven at all.

Printed as each entry is built (toggle: `chat_sessions.LOG_TURNS`, default `True`) — e.g.:

```
[turn 3] Mehmet: I feel so tired lately
    pushed 2 triple(s):
      feel tired  experiencer  =  I
      feel tired  time  =  lately
    gap query: subject=http://cltl.nl/leolani/n2mu/chat1.2 threshold=0.30 found(B=0, D=1, E=2) after_dedup=3
    selected gap: subject=http://cltl.nl/leolani/n2mu/chat1.2 predicate=http://cltl.nl/leolani/n2mu/location kind=predicate_object_type peer_coverage=3/4
```

Always populated into `self.turn_log` regardless of the toggle. `kg_chat_gui.py`'s
`save_session()` writes it out as a third timestamped JSON file (`chat<chat>_gaplog_<stamp>.json`)
alongside the turns/stats ones on quit, when driving the session through that GUI — `None` for a
plain `ChatSession`, which has no `turn_log` at all.

```python
from kg_chat_gui import run_gui

kg_session = KgChatSession(
    chat=1, human="Mehmet",
    kg_address="http://localhost:7200/repositories/event_sandbox",
)
run_gui(kg_session)   # opens the chat window; type as the human, the agent replies each turn
```

Two directories with a same-named flat module (`events_from_chat/prompts.py` and
`chat_from_kg/prompts/`) both need to be on `sys.path` for their respective imports to work, so
`chat_sessions._load_kg_dependencies()` imports `events_from_chat`'s modules first, clears its
`prompts` from `sys.modules`, *then* adds `chat_from_kg` to `sys.path` — importing both at once
would let whichever lands first on `sys.path` silently shadow the other's `prompts`. This runs
once, lazily, the first time a `KgChatSession` is constructed (and is cached after that), not
merely from importing `chat_sessions` or `KgChatSession` itself.

### Timeouts

Every OpenAI call in the whole chat/KG chain — `LLM_EventExtraction`'s extraction,
`LLMTripleReplier`'s reply/gap-question/confirmation-classification/acknowledgement generation,
and the plain default `agent_fn` reply — is built with `timeout=DEFAULT_OPENAI_TIMEOUT` (60s)
and `max_retries=0`, so a stuck or very slow request raises `openai.APITimeoutError` within that
one bounded window instead of blocking indefinitely (`max_retries=0` specifically so it surfaces
right at 60s, not up to 3× that if the SDK's own default retrying kicked in first). This is a
real fix for a real failure: a live chat turn was once observed hanging for 5+ minutes with no
error at all, stuck on exactly one of these calls, before this was added.

`chat_sessions._call_openai(source, fn, *args)` wraps every one of those call sites and
translates that `APITimeoutError` into a `ChatTimeoutError(source)` — `source` a short,
plain-language name of the step that stalled (e.g. `"extracting meaning from your message"`,
`"generating a knowledge-graph follow-up question"`, `"acknowledging your answer"`). `say()`
(both `ChatSession` and `KgChatSession`) catches it and returns
`self._timeout_reply(exc)` as the agent's turn instead — *"Sorry, there has been a timeout while
\<source\> — I didn't get a response in time. Could you please enter your message again?"* —
recorded in `self.turns`/shown in the chat window like any other reply, tagged `"timeout"` in
`reply_sources`, asking the human to just resend their message rather than leaving them staring
at a "thinking" indicator that will never resolve.

A few things this does and doesn't cover:

- **A timeout while classifying a pending agent-confirmation reply restores
  `self._pending_confirmation`** (see [Agent gaps: confirmation instead of an open
  question](#agent-gaps-confirmation-instead-of-an-open-question)) before the error reaches
  `say()`, so retrying is still routed as answering that same confirmation — not treated as an
  unrelated fresh utterance. A timeout while *acknowledging* an already-pushed confirm/correction
  does **not** restore it — the KG write already happened by then; only the acknowledgement text
  is missing.
- **A timeout annotating the agent's own successful reply** (the last step of `say()`, purely
  background bookkeeping for future gap-finding/extractor context) is handled separately and
  more leniently: it's only logged to stdout (`[KgChatSession] timed out ...`), never allowed to
  discard an already-delivered, already-successful reply.
- **Only OpenAI calls are covered.** The SPARQL calls this chain also makes (`kg_gap_finder`,
  `populate_ekg_from_annotations`, the graph panel's `fetch_triples()`) have no timeout of their
  own here — GraphDB responded in well under a second in every check made while building this,
  so it wasn't the source of the observed hang, but a genuinely stuck GraphDB instance isn't
  covered by any of this.

### Split-screen graph panel

When `session.kg_address` looks like a GraphDB repository endpoint (`.../repositories/<id>`,
checked by `graphdb_base_and_repository()`), `ChatWindow` splits into two panes with a
`ttk.PanedWindow`: the chat on the left (everything described above), and a **graph panel** on
the right — an actual node-link diagram, drawn in-window on a `tk.Canvas`, centered on whatever
activity the conversation is currently about: that activity as one node in the middle, and every
`(predicate, object)` triple pushed for it as a labeled edge fanning out to its own object node
(`_render_graph()`). For a plain `ChatSession`, or a `kg_address` that isn't a GraphDB repository
URL, the panel is skipped entirely and the window looks exactly as before.

**Why a drawn diagram instead of an embedded live graph:** GraphDB's own **Visual graph**
(`graphs-visualizations?uri=<activity>&role=context&repositoryId=<repo>`) is an interactive
D3/force-directed view running as JavaScript in a browser, and Tkinter has no embeddable browser
or JS engine to reproduce that specific view in-window (`tkinterweb` is HTML/CSS only, no JS; a
real Chromium embed means CEF, which is heavy and essentially unmaintained). So rather than a
broken or fake embed of *that* view, the panel draws its own simple node-link diagram straight
onto a `Canvas` from the same underlying data — every triple with the current activity as
subject, fetched with one dependency-free SPARQL query (`fetch_triples()`: plain `urllib`, no
rdflib/SPARQLWrapper, so this stays usable without any of `KgChatSession`'s heavier
dependencies) — and the **Open in GraphDB ↗** button is still there for the real, fully
interactive view (drag nodes, expand further, ...) in a browser tab whenever that's wanted.

- **The diagram itself** — the activity as a filled circle at the center, one line per triple to
  a smaller circle for its object, the predicate's local name on the line, the object's own
  label (or value, for a literal) inside its circle; long labels are truncated (`_truncate()`) to
  fit. Two kinds of triple are excluded from that (`drawable_triples` in `_render_graph()`):
  `rdfs:label` — the label value is already what the center node's own text shows
  (`_apply_graph_update()`'s `LABEL_PREDICATE` lookup), so drawing "label → *that same text*"
  again as its own node/edge would just repeat it, and a subject can legitimately carry several
  (one per turn that introduced a new phrase for it), which would otherwise show up as several
  near-duplicate nodes — and `gaf:denotedIn`/`denotedBy` (`GAF_PROVENANCE_PREDICATES`) — pure
  extraction provenance (which utterance span a fact came from), not semantic content, and
  typically several per subject too (one per turn that ever mentioned it), so drawing them would
  clutter the diagram with utterance-span nodes nobody asked about. Redrawing on a pane resize
  (`<Configure>`) reuses the already-fetched data
  (`self._graph_center_label`/`self._graph_center_uri`/`self._graph_triples` — the *raw*,
  unfiltered set; both kinds are filtered again on every redraw, not dropped from the cache) —
  no extra network round-trip just to re-lay-out the same graph at a new size.
- **Node colors, by RDF namespace** (`_namespace_of()`/`_node_fill_color()`) — red for `n2mu`,
  blue for `gaf`, green for `grasp` (`NAMESPACE_COLORS`, this project's own ontology's three
  namespaces — see `cltl.brain.infrastructure.rdf_builder._define_namespaces()`; `grasp` also
  covers its `grasp/factuality`, `grasp/sentiment`, `grasp/emotion`, `grasp/level`
  sub-namespaces), gray for a plain literal object (no URI at all), and any *other* namespace one
  of `OTHER_NAMESPACE_PALETTE`'s colors — assigned the first time that namespace is seen and
  cached per-window (`self._namespace_colors`), so it stays the same color for the rest of the
  session rather than shuffling on every redraw. A small legend under the activity dropdown shows
  the three fixed colors plus one "other" swatch.
- **Text size ("Font size" in the graph panel)** — its own slider (`DEFAULT_FONT_SIZE`, 16pt by
  default, adjustable independently of the conversation's own "Text size" slider — see the module
  docstring) drives the node/edge/predicate label sizes, and the node/center circle radii
  *roughly proportionally* with it (`node_r = font_size * 1.8`, `center_r = font_size * 2.4`,
  each floored) — a fixed add-on barely grows the circle across the slider's range while the
  text inside keeps growing, so this keeps a label wrapping onto roughly the same number of
  lines at any size instead of wrapping more and more as the slider goes up; the layout radius
  between the center and peripheral nodes grows with them too, so bigger nodes don't crowd each
  other or clip off the canvas edge. Changing it just re-runs `_render_graph()` on the
  already-cached data, exactly like a pane resize — no re-fetch.
- **Activity dropdown** — every activity subject URI mentioned so far, most-recent-first,
  labeled by its `rdfs:label` where the fetched triples have one (else a human-ish tail of the
  URI, `_local_name()`). Picking one re-fetches and redraws *its* diagram, on a background
  thread so switching never freezes the window.
- **Open in GraphDB ↗** — opens `graphdb_visualization_url(kg_address, selected_uri)` in the
  default browser (`webbrowser.open(..., new=0)`, so browsers that support it try to reuse an
  existing tab rather than piling up a new one per click).
- **auto-open new activities** (off by default) — automatically opens the browser the *first*
  time each new activity subject appears (`self._opened_subject_uris`, never repeats for one
  already opened this session), so the real interactive view opens itself as the conversation
  goes instead of needing a click every time.

The panel refreshes after every turn: `_refresh_graph_data()` reads
`session.last_subject_uris` (the KG subject URI(s) the turn just pushed, or the confirmation flow
just filled a gap on — see `chat_sessions.KgChatSession`'s own docstring) on the *same* background
thread that ran `session.say()`, so the extra SPARQL round-trip never blocks the UI; a failed
fetch (GraphDB down, a stale/unreachable `kg_address`, ...) is shown in a small status line under
the panel and never breaks the chat turn itself.

Starting the window with a `session` that already has activities (`session.last_subject_uris`
non-empty) primes the panel from a background thread too, so it isn't left empty until the next
turn -- but starting *that* thread is itself deferred via `root.after(0, ...)` from `__init__`
rather than fired immediately: `__init__()` runs before `run()`'s `root.mainloop()` call, and
Tkinter rejects a background thread's own `.after()` call ("main thread is not in main loop") if
it happens to finish before `mainloop()` has actually started -- easy to hit here since a local
GraphDB fetch can complete in a few milliseconds. Deferring the thread's *start* via
`root.after(0, ...)`, itself always safe to call before `mainloop()`, guarantees the loop is
already running by the time that thread's own `.after()` call happens.

## Setup

- **Python deps**: `requirements.txt` (torch/transformers/sentence_transformers are only needed
  for `perspective/`'s emotion detector, which the activity-id-based pipeline used here doesn't
  call — but `populate_ekg.py` imports it unconditionally at module load, so they still need to
  be installed).
- **`OPENAI_API_KEY`**: read at *import time* by `llm_event_triples_openai_pydantic.py` (module
  level) — set it before importing, not just before calling anything.
- **A SPARQL endpoint**: `populate_ekg_from_annotations` and `kg_gap_finder.analyze` both need
  one reachable (e.g. a local GraphDB `event_sandbox` repository). `KgChatSession` fails loudly
  if it isn't.
- **Ollama** (optional): only needed if you construct an `LLMTripleReplier(backend="ollama")`
  instead of the default `backend="openai"` used by `KgChatSession`.
- **Tkinter**: the live-chat cells open a window via `kg_chat_gui.py`, so a display is needed
  (part of the standard library on most Python installs; some Linux/pyenv builds need a separate
  `python3-tk`/`tk-dev` package).

## Known rough edges

- **`annotate_new_turn` costs one OpenAI call per turn**, but nothing amortizes across
  conversations — a fresh `LLM_EventExtraction()` per `KgChatSession` is the norm; call
  `reset_conversation()` if you reuse one for a second, unrelated conversation.
- **`gap_threshold` needs tuning to your graph's density.** A gap only counts once at least that
  fraction of an activity's *peer group* (see `find_comparison_peers()` above — typically a
  small, targeted set, not every same-type instance in the graph) share the predicate it's
  missing; `kg_gap_finder`'s own default is `0.6`, but `KgChatSession` defaults to `0.3` because
  a small peer group rarely reaches a strict majority on anything. Pass `gap_threshold=` to
  change it.
- **Test data lands in your real graph, and reusing a fixed `chat` id actively corrupts it.**
  `populate_ekg_from_annotations` always writes to `kg_address` — there's no dry-run mode. Each
  activity gets a KG subject URI of the form `.../n2mu/chat<chat>.<N>`, where `N` is just a
  per-conversation counter that a brand-new `KgChatSession`/`LLM_EventExtraction` always restarts
  at 1 — it has no way to know what an *earlier, unrelated* run already wrote to the graph under
  that same `chat` id. So a **hardcoded, reused `chat` (e.g. `chat=999999` on every run) doesn't
  give every run its own fresh activities — it makes every run's first activity land on the exact
  same URI as every other run's first activity** (same for the second, third, ...), silently
  merging separate conversations' triples onto shared nodes. Symptoms this produces later, often
  far from the actual cause: an activity's `rdfs:label` has several unrelated values at once (a
  real example that came up during development: `chat999999.1` ended up labeled both `"tired"`
  and `"dinner"` from two different sessions run hours apart), the graph panel appears to show
  "the wrong conversation" even though `KgChatSession`/`last_subject_uris` are working correctly,
  and peer-comparison gap-finding sees bloated, irrelevant peer groups (`find_comparison_peers()`
  pulling in dozens of unrelated instances instead of a small targeted set). **Generate `chat`
  fresh per run** (`notebooks/kg_chat_session.ipynb` now uses `int(time.time())`) rather than a
  fixed constant, and reuse one on purpose only when you deliberately want to *continue* one
  specific earlier conversation and are sure nothing else has ever run with that id. To clean up
  test data (fresh-id or not) afterward: `DELETE {?s ?p ?o} WHERE {?s ?p ?o . FILTER(CONTAINS(STR(?s),
  "<chat>") || CONTAINS(STR(?o), "<chat>"))}` — though a handful of shared/reusable vocabulary
  triples (e.g. predicate-type declarations) won't match that filter, since they're not
  test-specific; a node already corrupted by id collisions needs its *unwanted* triples picked out
  by hand instead (the filter above would delete the whole merged node, wanted triples included).
- **`_gap_queues`/`_asked_gap_keys`/`_pending_confirmation` are in-memory only**, per
  `KgChatSession` instance — recreating the session (e.g. a fresh notebook kernel) forgets which
  gaps were already asked, so a gap the graph still lacks a value for can resurface even though
  it was asked (and denied-without-correction) in an earlier session.
- **A confirmation turn costs two extra LLM calls** on top of the usual per-turn ones: one to
  classify the reply (`get_prompt_for_confirmation_response`), one to phrase the acknowledgement
  or fallback question. `_push_gap_triple` also always types a confirmed/corrected agent as
  `RoleType.person` — reasonable for "my son"/"my daughter"-style corrections, but not checked
  against what the correction actually says.
- **The graph panel's plain-text triples aren't the actual GraphDB Visual graph** — see [Split-
  screen graph panel](#split-screen-graph-panel) for why a real embed isn't practical in Tkinter;
  "Open in GraphDB ↗" is the only way to see the real, interactive view. It also adds one SPARQL
  round-trip per turn (`fetch_triples`) — cheap, but one more thing that can be slow/unreachable
  alongside the OpenAI/gap-lookup calls already on that same background thread. `auto-open new
  activities` defaults off and `self._opened_subject_uris` is a fresh, in-memory, per-window set
  either way, so a closed and reopened window has forgotten what it already auto-opened; turning
  the option back on there can re-open a browser tab for an activity it already opened before.
