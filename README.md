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

Driven from short notebooks under `notebooks/` — **[`chat_session.ipynb`](notebooks/chat_session.ipynb)**
(plain chat), **[`kg_chat_session.ipynb`](notebooks/kg_chat_session.ipynb)** (KG-populating chat,
peer-statistics gaps), **[`kg_intent_chat.ipynb`](notebooks/kg_intent_chat.ipynb)** (KG-populating
chat, hand-authored **intent**-driven gaps instead) and
**[`kg_catchup_intent_chat.ipynb`](notebooks/kg_catchup_intent_chat.ipynb)** (the intent-driven
chat, but driven by a saturation loop that keeps asking about "what's happened since we last
spoke?" topics until it judges it has enough for the gap period) — all importing
`ChatSession`/`KgChatSession`/`KgIntentChatSession` from
**[`notebooks/chat_sessions.py`](notebooks/chat_sessions.py)**, which in turn wires together the
two independent pipelines living under `src/cltl/`. All four notebooks drive their live chat
through **[`notebooks/kg_chat_gui.py`](notebooks/kg_chat_gui.py)**: a single Tkinter window
showing the whole transcript, with the human typing into that same window instead of the
notebook's own `input()` prompt.

## Contents

- [Quick start](#quick-start)
- [Pipeline overview](#pipeline-overview)
- [The turn schema](#the-turn-schema)
- [`src/cltl/events_from_chat/` — chat → semantic roles → knowledge graph](#srclcltleventsfromchat--chat--semantic-roles--knowledge-graph)
- [`src/cltl/chat_from_kg/` — knowledge graph → gap → reply](#srclcltlchatfromkg--knowledge-graph--gap--reply)
- [Intent-driven gaps: `intent_gap_finder.py`](#intent-driven-gaps-intent_gap_finderpy)
- [`notebooks/` — putting it together](#notebooks--putting-it-together)
- [Catch-up opening flow: `catch_up_from_kg.py`](#catch-up-opening-flow-catch_up_from_kgpy)
- [Setup](#setup)
- [Known rough edges](#known-rough-edges)

## Quick start

1. Install `requirements.txt` into a virtualenv (`.venv` is already set up in this repo).
2. Have a GraphDB (or other SPARQL 1.1) repository running and reachable, e.g.
   `http://localhost:7200/repositories/event_sandbox` — needed for every notebook except
   `chat_session.ipynb`.
3. `export OPENAI_API_KEY=...` (required — several modules read it at *import* time, not just
   when a client is constructed).
4. Open one of the notebooks and run the cells top to bottom:
   - `notebooks/chat_session.ipynb` — plain chat, needs OpenAI only (or nothing at all with the
     built-in `mock_agent`).
   - `notebooks/kg_chat_session.ipynb` — KG-populating chat whose follow-up questions come from
     `kg_gap_finder.py`'s peer-statistics gaps.
   - `notebooks/kg_intent_chat.ipynb` — the same KG-populating chat, but follow-up questions come
     from hand-authored **[`intents/`](intents/)** definitions instead (see
     [Intent-driven gaps](#intent-driven-gaps-intent_gap_finderpy)) — works even for the very
     first activity of a kind ever pushed to the graph, unlike the peer-statistics version.
   - `notebooks/kg_catchup_intent_chat.ipynb` — the intent-driven chat above, but driven by a
     saturation loop: it queries the graph for when this human last talked and how often they
     typically report each topic, then keeps asking about gap-period topics — interleaved with
     the intent-driven follow-ups above for whatever's reported — until it judges it has enough
     for the gap period (see [Catch-up opening
     flow](#catch-up-opening-flow-catch_up_from_kgpy)) — most useful against a graph that already
     has some history for the human in question.

   All four need the knowledge graph except `chat_session.ipynb`. The live-chat cell in each opens
   its own window (needs a display and Tkinter — see
   [`notebooks/kg_chat_gui.py`](notebooks/kg_chat_gui.py)); type there, or type "bye" (or click
   **Quit**) to end the conversation and let the cell finish. Quitting writes the transcript and a
   statistics summary to two timestamped JSON files next to the notebook and prints their paths.

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

## Intent-driven gaps: `intent_gap_finder.py`

`kg_gap_finder.py`'s own gaps (above) only fire once a **majority** of an activity's peers already
have the predicate in question — so the very first `take_food` activity ever pushed to the graph
can never produce one, even though a diet-coaching intent obviously wants to know what was eaten.
**[`src/cltl/chat_from_kg/intent_gap_finder.py`](src/cltl/chat_from_kg/intent_gap_finder.py)**
sidesteps that: instead of deriving "what's expected" from peer statistics, it reads it straight
from a hand-authored **intent** — one JSON object per `data_type.ActivityType` value (`take_food`,
`exercise`, `physical_condition`, ...), loaded from one `*.json` file per topic under
**[`intents/`](intents/)** at the project root (`diet_intents.json`, `exercise_intents.json`,
`condition_intents.json`, `medication_intents.json`, `measurement_intents.json`,
`sleep_intents.json`, `symptom_intents.json`) — independent of how many (if any) similar
activities already exist in the graph.

An intent looks like this (`intents/diet_intents.json`, trimmed):

```json
{
  "activity_types": ["take_food"],
  "patient_type": ["food"],
  "patient_question": "What do you have for {activity}?",
  "activity_date": "date",
  "date_question": "When did you have your {activity}",
  "secondary_objectives": {
    "patient_qualification": "quantity",
    "qualification_question": "how much {patient} did you have?"
  }
}
```

`next_intent_gap()` checks an intent's own requirements in a **fixed priority order**, stopping at
(and returning) the first one not yet met — "the data elements for the intent must first be met
before moving on with the conversation":

1. **`patient_type`** — the activity must have a `patient` of one of these role-filler types (what
   was eaten/drunk/taken/measured, e.g. `["food"]`, `["medication"]`).
2. **`activity_qualification`** — one `qualification` value per named aspect, in order (e.g.
   `["duration", "degree"]` for a condition — first "how long", then "how strong", never both at
   once, since the graph model has no way to tell which existing `qualification` value answers
   which aspect other than the order they were asked in).
3. **`activity_date`** — any `time` value at all.
4. **`secondary_objectives`** — only reached once 1–3 are *all* satisfied; each `{key: value}` is
   another `*_qualification`/`*_location`/`*_date`/generic-predicate check, tried in the object's
   own order (e.g. diet's `"patient_qualification": "quantity"` — *how much* — asked only once
   *what* was eaten and *when* are both already known).

Every requirement may carry its own hand-authored `*_question` example (`patient_question`,
`qualification_question` — a single template or a `{aspect: template}` mapping, `date_question`,
`location_question`) with `{activity}`/`{patient}`/`{patient_type}` placeholders — the LLM
paraphrases *that* instead of inventing a question from the bare subject/predicate/type triple,
which is what actually stops a weaker LLM backend from leaking a raw role name like "patient" into
the question it asks.

**`activity_labels`** disambiguates several intents that share one `activity_types` entry by the
activity's own label/phrase instead — needed because `data_type.ActivityType` has only one generic
`symptom` type covering many distinct real symptoms. `symptom_intents.json` declares five separate
intents all typed `symptom`, each naming the one symptom phrase (`"headache"`, `"dizziness"`, ...)
its own `activity_labels` covers; if a symptom's phrase doesn't match any of them, `find_intent()`
deliberately returns no match (rather than guessing one) so nothing asks about, say, a `body_part`
location that doesn't apply to whatever the real symptom turns out to be.

**No intent covers this activity type at all** (or none of several label-specific candidates
matched) → the same "fall back to the plain LLM reply" behaviour as `kg_gap_finder.py` finding
nothing.

### Why the same question doesn't repeat

The incremental SRL extractor doesn't reliably coreference a short follow-up reply ("30 minutes",
"after meals", "no") back onto the SAME activity/role an intent's question was actually about —
often it mints a brand-new, near-empty subject for it instead. Since a fresh subject has no gap
history of its own, the identical requirement looked "never asked" for it and fired again — the
same question, verbatim or reworded, could repeat indefinitely. Two mechanisms fix this, both in
`KgChatSession`/`KgIntentChatSession` (`notebooks/chat_sessions.py`):

- **Direct answer capture.** A gap row may carry a `fill_role` (e.g. `"patient"`, `"location"`,
  `"qualification"`) — set by `intent_gap_finder.py`'s gap builders only when there's a single,
  unambiguous real predicate to write to (see `_make_gap()`'s own docstring; a multi-variant "date"
  gap deliberately never gets one). Asking such a gap arms `self._pending_intent_answer`, so the
  human's very next reply is classified (`get_prompt_for_intent_answer_response()`) as one of:
  - **ANSWER: \<value\>** — pushed directly onto the pending gap's own subject/role
    (`_push_gap_triple()`), skipping the general extractor entirely, then acknowledged.
  - **DECLINE** — nothing pushed; acknowledged and dropped, not re-asked.
  - **UNRELATED** — e.g. a clarifying question back ("what body function?") — falls through to
    the ordinary annotate-and-push/gap-finding flow, exactly as if there had been no pending
    answer at all.
- **`MAX_INTENT_GAP_ATTEMPTS` backstop.** `KgIntentChatSession._fetch_gap_queue()` also tracks how
  many times each underlying requirement — keyed by `(id(intent), kind, gap["predicate"])`, so
  it's the same key no matter which (possibly freshly-minted, unrelated) subject surfaces it — has
  been surfaced this chat. Once that hits `MAX_INTENT_GAP_ATTEMPTS` (2), the intent gives up on it
  silently for the rest of the session (logged as `gave_up: True` in `turn_log`'s `gap_queries`
  entries) — the safety net for whatever the classifier above still gets wrong.

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
| `chat_sessions.py` — `KgChatSession` | The full KG loop, peer-statistics gaps (see below). |
| `chat_sessions.py` — `KgIntentChatSession` | Same loop, but follow-up questions come from `intent_gap_finder.py` instead — see [Intent-driven gaps](#intent-driven-gaps-intent_gap_finderpy) and below. |
| `catch_up_from_kg.py` | `SaturationTracker` — the "keep asking about gap-period topics until we have enough" loop, built on `gaps_from_kg/get_temporal_containers.py` — see [Catch-up opening flow](#catch-up-opening-flow-catch_up_from_kgpy). |
| `kg_chat_gui.py` | `run_gui(session)`: opens one Tkinter window (`ChatWindow`) for a `ChatSession`/`KgChatSession` — the whole transcript scrolls on the left, and the human types into an entry box built into the *same* window (no `input()`, no popup). Each agent turn is tagged **[KG]**/**[LLM]** from `session.reply_sources` (a plain `ChatSession` has none, so it's always **[LLM]**). For a `KgChatSession`, a **Gap sensitivity** slider adjusts `session.gap_threshold` live, mid-conversation — dragging it also drops any already-cached per-instance gap queues, so the new sensitivity takes effect on the very next gap lookup rather than only once whatever was already queued happens to drain (`_on_gap_threshold_change`); not shown for a plain `ChatSession`, which has no `gap_threshold`. A **Text size** slider (always shown, 16pt default) live-resizes the whole conversation area at once via shared `tkinter.font.Font` objects. When `session.kg_address` is a GraphDB repository, the window splits and a live **graph panel** appears on the right (see below), with its own independent **Font size** slider. Talking to the graph/LLM runs on a background thread per turn so the window never freezes; the entry box is never disabled, so a quit word ("bye" etc., checked first in `_on_send`) or the always-enabled **Quit** button closes the window immediately even if a reply is stuck (`_on_close` calls `root.quit()` before `root.destroy()` — destroy alone doesn't reliably end a `mainloop()` that Jupyter is driving, which left the window up and the cell hanging). Quitting also writes the transcript and a statistics summary to timestamped JSON files via `save_session()`/`session_statistics()` — plus, for a `KgChatSession`, its per-turn `turn_log` (see below) as a third file — (`save_dir=None` disables). Returns `session.turns`, so it's a drop-in replacement for `run_interactive()` in a notebook cell. |
| `chat_session.ipynb` | Imports `ChatSession`/`mock_agent`/`simulate_chat`/`save_turns`/`run_gui` and runs a plain chat: live (`run_gui(ChatSession(...))`) or scripted (`simulate_chat()`), then inspects/saves the resulting turns. |
| `kg_chat_session.ipynb` | Imports `KgChatSession`/`run_gui`, sets `KG_ADDRESS`/`KG_LOG_DIR`, and runs a live KG-populating chat, then inspects `annotations`/`kg_pushes`/`reply_sources`. |
| `kg_intent_chat.ipynb` | Same as `kg_chat_session.ipynb`, but constructs `KgIntentChatSession` (`intents_dir=` defaults to the project's own `intents/`) instead of `KgChatSession` — no `gap_threshold`/`gap_activity_types` (not meaningful here; see [Intent-driven gaps](#intent-driven-gaps-intent_gap_finderpy)) — and its `turn_log` prints an `intent gap query: ...` line (matched intent + `activity_type`) instead of `kg_gap_finder`'s peer-vote breakdown. |
| `kg_catchup_intent_chat.ipynb` | Builds a `KgIntentChatSession` exactly like `kg_intent_chat.ipynb`, but with its `agent_fn` wrapped by `catch_up_from_kg.wrap_agent_fn_with_saturation_loop()` and `on_new_subject=tracker.record_new_activity`, and opens with `SaturationTracker.opening_question()` before starting the live chat — see [Catch-up opening flow](#catch-up-opening-flow-catch_up_from_kgpy). |

### `KgChatSession(ChatSession)`

For every new **human** turn, `say()` branches three ways on whether the *previous* agent turn
was a pending gap question of one of two kinds (both armed by `_reply_from_gaps` — see [Agent
gaps: confirmation instead of an open
question](#agent-gaps-confirmation-instead-of-an-open-question) and [Why the same question
doesn't repeat](#why-the-same-question-doesnt-repeat) above):

- **An agent-confirmation is pending** (`self._pending_confirmation`) — routed to
  `_handle_confirmation_reply()` instead of the general SRL pipeline: it classifies the reply,
  then either pushes the confirmed/corrected triple straight to the KG (`_push_gap_triple`,
  bypassing `LLM_EventExtraction` entirely — it isn't built to make sense of a bare "yes") or, on
  a plain denial, falls back to the open question for the same gap.
- **A non-agent-like intent answer is pending** (`self._pending_intent_answer`, only ever armed
  for an `intent_gap_finder.py` gap with a `fill_role`) — routed to
  `_handle_intent_answer_reply()`: ANSWER pushes the value onto the gap's own subject/role and
  acknowledges (same idea as confirmation, generalized); DECLINE acknowledges and drops the
  requirement (deliberately does *not* re-ask, unlike a plain agent denial); UNRELATED falls
  through to the normal flow below, exactly as if nothing had been pending.
- **Otherwise**, the normal flow:
  1. Immediately runs `LLM_EventExtraction.annotate_new_turn(...)` on the turn and, if it
     produced anything, pushes it into the graph with `populate_ekg_from_annotations(...)`
     (`_annotate_and_push`, returning the KG subject URI(s) of whatever activity/condition was
     just asserted).
  2. **If new triples were pushed**, `_reply_from_gaps` asks `_next_gap` for the next gap to ask
     about around those subject URIs and, via `LLMTripleReplier`, turns it into the agent's
     reply — arming `self._pending_confirmation` or `self._pending_intent_answer` depending on
     the gap.
  3. **Otherwise** (no new triples, or no gap left to ask) falls back to the default `agent_fn`
     reply, same as plain `ChatSession`.

Either way, the agent's reply is then recorded as its own turn and annotated + pushed too (so the
extractor's running context includes what the agent said, same as the batch pipeline would) —
except a confirmation-reply or intent-answer-reply turn's *human* turn skips that generic
annotation, for the same reason it skipped it going in.

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

### `KgIntentChatSession(KgChatSession)`

Swaps only the gap-*finding* step for `intent_gap_finder.py` (see [Intent-driven
gaps](#intent-driven-gaps-intent_gap_finderpy)) — everything else (annotating/pushing turns,
agent-confirmation handling, timeouts, the rest of `turn_log`) is unchanged, since an intent's
answer is annotated back onto the graph exactly the same way an ordinary `kg_gap_finder` gap's
answer is:

- **`_is_gap_eligible_type(subject_uri)`** — eligible exactly when `intent_gap_finder.find_intent()`
  finds a matching intent for that subject's activity type (and, if several intents share it, its
  own label — e.g. several distinct symptoms) — instead of `KgChatSession`'s
  `gap_activity_types` allow-list.
- **`_fetch_gap_queue(subject_uri)`** — one `intent_gap_finder.next_intent_gap()` call for the
  matching intent instead of a peer-statistics `kg_gap_finder.analyze()` call. An intent's own
  checks already run in priority order and stop at the first unmet one, so the "queue" this
  returns is always length 0 or 1 — `_next_gap()` drains it exactly the same way regardless.
- **`turn_log`'s `gap_queries` entries** are shaped `{"subject", "activity_type",
  "intent_source", "after_dedup", "gave_up"}` (which `intents/*.json` file matched, if any, and
  whether `MAX_INTENT_GAP_ATTEMPTS` kicked in — see below) instead of `kg_gap_finder`'s peer-vote
  fields (`threshold`/`found`) — there's no peer voting to report here.

`self.intents` holds the loaded intent definitions (`intent_gap_finder.load_intents()`); an
activity type covered by none of them is never gap-driven — its turns are still
annotated/pushed to the graph like any other, but the agent's reply for it always falls back to
the plain default `agent_fn`.

**The same intent question doesn't repeat forever** — see [Why the same question doesn't
repeat](#why-the-same-question-doesnt-repeat) above for the full mechanism
(`KgChatSession._pending_intent_answer`/`_handle_intent_answer_reply()` plus this class's own
`self._intent_requirement_attempts`/`MAX_INTENT_GAP_ATTEMPTS`).

```python
from kg_chat_gui import run_gui

kg_session = KgIntentChatSession(
    chat=1, human="Mehmet",
    kg_address="http://localhost:7200/repositories/event_sandbox",
    intents_dir=None,   # None auto-discovers the project's own intents/ folder
)
run_gui(kg_session)
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
  is missing. `self._pending_intent_answer`/`_handle_intent_answer_reply()` (see [Why the same
  question doesn't repeat](#why-the-same-question-doesnt-repeat)) follows the identical pattern.
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
  fit. A few kinds of triple are excluded from, or merged within, that
  (`drawable_triples` in `_render_graph()`):
  `rdfs:label` — the label value is already what the center node's own text shows
  (`_apply_graph_update()`'s `LABEL_PREDICATE` lookup), so drawing "label → *that same text*"
  again as its own node/edge would just repeat it, and a subject can legitimately carry several
  (one per turn that introduced a new phrase for it), which would otherwise show up as several
  near-duplicate nodes — `gaf:denotedIn`/`denotedBy` (`GAF_PROVENANCE_PREDICATES`) — pure
  extraction provenance (which utterance span a fact came from), not semantic content, and
  typically several per subject too (one per turn that ever mentioned it), so drawing them would
  clutter the diagram with utterance-span nodes nobody asked about — and `sem:eventProperty`/
  `eps:contextProperty` (`GENERIC_ANCESTOR_PREDICATES`) — the SEM/episodic-awareness ontologies'
  own generic ANCESTOR properties every real role predicate is `rdfs:subPropertyOf` (transitively,
  via `n2mu_sem_roles.py`'s own mapping — see [Catch-up opening
  flow](#catch-up-opening-flow-catch_up_from_kgpy)): on a GraphDB repository with RDFS/OWL
  inference enabled, every real triple (e.g. `n2mu:agent`) is therefore ALSO materialized under
  these two generic predicates to the exact same object, and `fetch_triples()` has no way to tell
  an inferred triple from an asserted one — without this exclusion, every single edge in the
  diagram would draw up to three times over (once for its own real predicate, once each for these
  two generic ones).

  **`sem:hasActor`/`hasPlace`/`hasTime` are a related but different case** — unlike the two
  generic ancestors above, these DO carry real meaning (they're SEM's own role vocabulary), so
  rather than dropping them outright, `_merge_equivalent_edges()`
  (`EDGE_EQUIVALENCE_GROUPS` — one group each for the agent-like roles `agent`/`agent_patient`/
  `participant`/`experiencer` + `hasActor`, for `location` + `hasPlace`, and for the four
  `time/*` variants + `hasTime`) collapses each one down to a SINGLE edge with whichever real
  `n2mu:` predicate shares the same object, instead of excluding it. The same RDFS/OWL inference
  that materializes `sem:eventProperty`/`eps:contextProperty` copies also materializes these —
  e.g. an `n2mu:agent -> "Jan"` triple gets an `sem:hasActor -> "Jan"` copy too — so without
  merging them, "Jan" would draw as two (or three, if `agent_patient` is also present) separate
  near-identical edges for what's really one fact. The first matching predicate actually present
  for a given object is what the merged edge is labeled with, so a real, specific `n2mu:`
  predicate always wins over its own generic `sem:` copy.

  Redrawing on a pane resize (`<Configure>`) reuses the already-fetched data
  (`self._graph_center_label`/`self._graph_center_uri`/`self._graph_triples` — the *raw*,
  unfiltered set; every exclusion/merge above is re-applied on every redraw, not dropped from the
  cache) — no extra network round-trip just to re-lay-out the same graph at a new size.
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

## Catch-up opening flow: `catch_up_from_kg.py`

`kg_intent_chat.ipynb` (and `kg_chat_session.ipynb`) both start a live chat cold — the human has
to bring up whatever they want to talk about themselves, and the conversation runs until they
stop, with no notion of "have we actually covered enough for the time that's passed." **[`notebooks/catch_up_from_kg.py`](notebooks/catch_up_from_kg.py)**
replaces that with a closed loop on top of `KgIntentChatSession`, driven from
**[`src/cltl/gaps_from_kg/get_temporal_containers.py`](src/cltl/gaps_from_kg/get_temporal_containers.py)**
— a *different* query layer over the same GraphDB repository (`cltl.brain.LongTermMemory`'s own
SPARQL layer, not `kg_gap_finder.py`'s rdflib one) — used only by `kg_catchup_intent_chat.ipynb`:

0. **`connect_brain(kg_address, log_dir)`** — connects, and calls **`ensure_role_hierarchy()`**:
   a one-time, idempotent SPARQL Update that uploads
   **[`gaps_from_kg/n2mu_sem_roles.py`](src/cltl/gaps_from_kg/n2mu_sem_roles.py)**'s
   `rdfs:subPropertyOf` mapping from this project's own fine-grained `n2mu:` SRL role predicates
   (`agent`/`agent_patient`/`participant`/`experiencer` → `sem:hasActor`, `location` →
   `sem:hasPlace`, the four `time/*` variants → `sem:hasTime`) to the SEM ontology roles
   `get_temporal_containers()`'s underlying query actually reads (see below). The actor-like set
   mirrors `kg_gap_finder.AGENT_ROLE_PREDICATES` exactly — `patient` itself is deliberately
   excluded, since it's what an activity acts *on*, not who acts.
1. **`find_last_conversation_date(human, brain, current_date, fallback_date)`** — wraps
   `get_temporal_containers.get_last_conversation_date()`: the most recent date this human is on
   record as having spoken at all, or `fallback_date` if the graph has none (e.g. a brand new
   human).
2. **`find_catch_up_topics(brain, current_date, recent_date)`** — first caps the period this
   session actually tries to catch up on at `MAX_SATURATION_GAP_DAYS` (**14 days**):
   `effective_recent_date = max(recent_date, current_date - 14 days)` — the *later* of the true
   last-conversation date or two weeks ago, so a human who last talked 3 days ago only needs those
   3 days covered, while one who hasn't talked in 2 months only needs the last 14. Then, for every
   activity/condition type `chat_sessions.DEFAULT_GAP_ACTIVITY_TYPES` the human has ever discussed
   before (see below), runs `get_temporal_containers.get_temporal_containers()` (with
   `effective_recent_date` as its own "recent_date", so its "history"/"gap" split lines up with
   the cap) and computes, per topic:
   - **`weekly_rate`** — this topic's typical **weekly** frequency: the average number of
     activities found per 7-day window, tiling that fixed week-long window backwards across the
     human's *entire* dated history before the catch-up period (`_windowed_average_rate()`, always
     called with a 7-day window regardless of how long the catch-up period itself is) — "the
     weekly average of activities and conditions reported in the past." `0.0` for a topic with no
     *dated* mentions at all (see the "unknown"-bucket fallback below).
   - **`expected_count`** — `weekly_rate` scaled to however many days the (capped) catch-up period
     covers (`weekly_rate * effective_gap_days / 7`, rounded, at least 1 whenever there's any
     dated history at all). A topic whose only mentions are undated (`weekly_rate` `0.0`) falls
     back to a plain `1` instead — this topic's own *saturation target* either way.
   - **`initial_reported_count`** — however many of this topic's activities are *already* in the
     KG's own "gap" bucket (dated within the capped catch-up period) before this conversation even
     starts, e.g. from another channel — credited toward the target instead of double-asked.

   **A topic counts as "discussed before" whenever EITHER `get_temporal_containers()`'s "history"
   *or* its "unknown" bucket is non-empty** — not "history" alone. In practice almost every
   activity's own `n2mu:time/*` role is a bare phrase ("for an hour", "recently") the SRL
   extractor never resolves to a real date, and `get_temporal_containers()` itself now falls back
   to the *conversation's own* date (`gaf:denotedIn` → `sem:hasBeginTimeStamp`, the same chain
   `get_role_relation_query()` already used) whenever an activity's own time value doesn't resolve
   — so a mention only lands in "unknown" in the rarer case where even that utterance-level
   timestamp is missing. Requiring "history" alone used to silently exclude almost every real
   topic except whichever one happened to have a cleanly-parseable date somewhere; a topic found
   only via "unknown" still gets a target, just the plain `expected_count = 1` fallback above,
   since there's no dated sample to compute a rate from.
3. **`SaturationTracker`** — holds every topic's target and how many have actually been reported
   *live* this session (seeded from `initial_reported_count`, updated via
   `record_new_activity(subject_uri, activity_type)` — matching `on_new_subject`'s own signature
   exactly, see below, so it can be passed straight through as-is):
   - **`opening_question(...)`** — one LLM call phrasing the actual first turn: how long it's
     been, inviting the human to share what's happened, naming the `lead_topics` (default 2)
     topics with the biggest shortfall as concrete memory prompts — and marks those as asked once,
     so the loop below doesn't immediately repeat them.
   - **`next_question()`** — asks about whichever still-*unsaturated* topic (short of its own
     `expected_count`, and not yet at the per-topic ask cap) has the biggest shortfall, rephrased
     as a natural follow-up ("anything else...") once a topic's already been asked about before,
     rather than the identical question again.
   - **`is_saturated()`** — True once every topic is at/above target or capped out (see below) —
     "enough knowledge for the gap period" (this module's stated goal), not "every target hit no
     matter what."
   - **`MAX_ASKS_PER_TOPIC`** (default 3) — a hard per-topic cap, independent of whether its own
     target was ever reached: a human with nothing more to say about a topic shouldn't be asked
     about it forever. Bounds the whole loop too (at most `len(topics) * MAX_ASKS_PER_TOPIC`
     catch-up questions, worst case).
   - **`wrap_up_message(...)`** — the ONE message sent the FIRST time every topic becomes
     saturated (or capped out): rather than this module going silent and the conversation just
     stopping with no closing turn, it tells the LLM what was actually covered this session and
     lets it decide for itself — continue with a short question if there's an obvious thread left,
     or wrap up warmly and say goodbye. Sets `self.wrapped_up` so it never fires a second time.
4. **`wrap_agent_fn_with_saturation_loop(agent_fn, tracker)`** — wraps a plain `agent_fn` so that
   every call to it (which `KgChatSession.say()` only ever makes once it's found no per-turn
   intent gap left to ask about — the `"default"` `reply_sources` case) asks about the next
   under-covered topic instead, for as long as `tracker.is_saturated()` is False; the first time
   every topic is saturated (or capped out), `tracker.wrap_up_message()` runs once instead of
   silently falling through; after that (`tracker.wrapped_up`), every call goes straight through
   to `agent_fn` unchanged.

The loop this produces: **ask about a gap-period topic** (`next_question()`) → whatever the human
reports is handled entirely by `KgIntentChatSession`'s own *existing* per-turn flow, completely
unchanged (SRL extraction → push to the KG → `intent_gap_finder.next_intent_gap()` keeps drilling
into that SAME activity's own what/how much/when/where for as long as its matching intent has
unmet requirements) → once that's exhausted and `say()` would fall back to the default reply, **go
back to asking about a topic** (the same one again if still short, or a different one) → **unless
`tracker.is_saturated()`**, in which case the LLM gets exactly one chance to wrap things up
(`wrap_up_message()`) before the conversation continues (or ends) as an ordinary chat from there.
Getting
`SaturationTracker.record_new_activity()` called for every genuinely new activity as it's pushed
— the *only* piece this needs from `chat_sessions.py` itself, since the rest is pure `agent_fn`
wrapping — is `KgChatSession`'s own `on_new_subject` constructor parameter: an optional
`callable(subject_uri, activity_type)` invoked exactly once per new `activity_id`, the first time
`_annotate_and_push()` ever sees a recognized type for it (never again on a later turn that just
adds another role to one already known).

```python
import catch_up_from_kg as catch_up
from chat_sessions import KgIntentChatSession, openai_agent

brain = catch_up.connect_brain(KG_ADDRESS, log_dir=KG_LOG_DIR)
last_date = catch_up.find_last_conversation_date("Mehmet", brain, CURRENT_DATE, FALLBACK_DATE)
topics = catch_up.find_catch_up_topics(brain, CURRENT_DATE, last_date)

tracker = catch_up.SaturationTracker(topics, human="Mehmet")
agent_fn = catch_up.wrap_agent_fn_with_saturation_loop(openai_agent(), tracker)

kg_session = KgIntentChatSession(
    chat=1, human="Mehmet", kg_address=KG_ADDRESS, agent_fn=agent_fn,
    on_new_subject=tracker.record_new_activity,
)
opening_question = tracker.opening_question(CURRENT_DATE, last_date, lead_topics=2)
kg_session.open_with(opening_question)

# ... run the chat, then:
catch_up.save_intent_log(kg_session, tracker, topics, CURRENT_DATE, last_date)
```

**`save_intent_log(kg_session, tracker, catch_up_topics, current_date, recent_date, log_dir=
"intents_log")`** — once the chat is over, writes one
`notebooks/intents_log/chat<chat>_intents_<stamp>.json` file (same timestamped-filename
convention as `kg_chat_gui.save_session()`'s turns/stats/gaplog files, now under
`notebooks/chat_logs/`) summarizing the whole session:

- **`gap`** — the true gap since the last conversation (`gap_days`) alongside the (possibly
  capped) period this session actually tried to saturate (`effective_recent_date`/
  `effective_gap_days` — see `MAX_SATURATION_GAP_DAYS` above).
- **`topics_at_start`** — `find_catch_up_topics()`'s own raw output: every topic identified as
  worth catching up on *before* the chat began, with its saturation target.
- **`topics_covered`** — how each of those topics actually fared *live* (reported/asked counts,
  whether its target was met or it was just capped out), `tracker.asked_log` (one entry per
  catch-up question actually asked, in order), and whether the tracker ended up saturated/wrapped
  up.
- **`intents_covered`** — every `intent_gap_finder.py` intent the per-turn flow actually consulted
  during the chat (which `intents/*.json` file, which activity type(s), how many times, whether
  `MAX_INTENT_GAP_ATTEMPTS` ever made it give up) — independent of the catch-up topics above,
  since an intent fires for *any* matching activity the human mentions, not just ones the
  saturation loop itself asked about.

**Why the role mapping is needed:** `gaps_from_kg/thought_util.py`'s `get_sem_relation_query()`
looks for `sem:hasActor`/`sem:hasPlace`/`sem:hasTime`, but no activity ever carries those directly
— `events_from_chat/events_to_capsules.py` only ever asserts this project's own finer-grained
`n2mu:` role predicates. Two bugs used to make this a dead end: the query wrote those sem: roles
as `<sem:hasActor>` (a prefixed name wrapped in angle brackets, which SPARQL parses as a literal,
never-matching IRI rather than expanding the `sem:` prefix), and even fixed, it still had no way
to know an `n2mu:agent` triple *means* `sem:hasActor`. Both are fixed now: the query uses the
prefixed names directly and matches `?p rdfs:subPropertyOf* sem:hasActor` (etc.) instead of the
literal predicate, and `ensure_role_hierarchy()` is what makes that path resolve — evaluated
directly against the uploaded mapping triples, so it works whether or not the repository has RDFS
reasoning enabled.

## Setup

- **Python deps**: `requirements.txt` (torch/transformers/sentence_transformers are only needed
  for `perspective/`'s emotion detector, which the activity-id-based pipeline used here doesn't
  call — but `populate_ekg.py` imports it unconditionally at module load, so they still need to
  be installed).
- **`OPENAI_API_KEY`**: read at *import time* by `llm_event_triples_openai_pydantic.py` (module
  level) — set it before importing, not just before calling anything.
- **A SPARQL endpoint**: `populate_ekg_from_annotations` and `kg_gap_finder.analyze` both need
  one reachable (e.g. a local GraphDB `event_sandbox` repository). `KgChatSession` fails loudly
  if it isn't. `catch_up_from_kg.py` needs the same endpoint already holding some history for the
  human in question, or its catch-up phase has nothing to surface (see [Catch-up opening
  flow](#catch-up-opening-flow-catch_up_from_kgpy)).
- **`intents/` at the project root**: only needed for `KgIntentChatSession`
  (`kg_intent_chat.ipynb`/`kg_catchup_intent_chat.ipynb`) — auto-discovered from the notebook's own
  working directory (`intent_gap_finder._default_intents_dir()`); pass `intents_dir=` to use a
  different set.
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
- **`_gap_queues`/`_asked_gap_keys`/`_pending_confirmation`/`_pending_intent_answer`/
  `_intent_requirement_attempts` are all in-memory only**, per `KgChatSession`/
  `KgIntentChatSession` instance — recreating the session (e.g. a fresh notebook kernel) forgets
  which gaps were already asked (or how many times each intent requirement was already surfaced),
  so a gap the graph still lacks a value for can resurface, and `MAX_INTENT_GAP_ATTEMPTS`'s count
  restarts from zero, even though it was asked (and denied-without-correction, or already given up
  on) in an earlier session.
- **A confirmation turn costs two extra LLM calls** on top of the usual per-turn ones: one to
  classify the reply (`get_prompt_for_confirmation_response`), one to phrase the acknowledgement
  or fallback question. `_push_gap_triple` also always types a confirmed/corrected agent as
  `RoleType.person` — reasonable for "my son"/"my daughter"-style corrections, but not checked
  against what the correction actually says. `_handle_intent_answer_reply()`'s generalized version
  (see [Why the same question doesn't repeat](#why-the-same-question-doesnt-repeat)) costs the same
  two extra calls, and its own classifier is a single LLM call with no retry — a reply it
  misclassifies as UNRELATED just falls through to the ordinary extractor (no worse than before
  this feature existed), but one misclassified as ANSWER pushes whatever value it extracted
  as-is, unchecked against the gap's own expected type. `MAX_INTENT_GAP_ATTEMPTS` (2) is a module
  constant, not yet a constructor parameter — change it in `chat_sessions.py` directly if a
  different chat needs a different cap.
- **An intent's `activity_types`/`activity_labels` must match the graph's own spelling exactly**
  (after `intent_gap_finder._normalize()`'s case/`-`/`_`/space folding) — an `intents/*.json` file
  covering a real `data_type.ActivityType` value under a slightly different spelling silently
  matches nothing, and that activity type just never gets an intent-driven follow-up (falls back
  to the plain LLM reply) with no error anywhere to flag the mismatch.
- **`ensure_role_hierarchy()`'s mapping is fixed, hand-picked, and additive-only.** It covers
  exactly the roles `get_sem_relation_query()` reads (actor-like roles, `location`, the four
  `time/*` variants — see [Catch-up opening flow](#catch-up-opening-flow-catch_up_from_kgpy)); a
  new SRL role added to `data_type.SemanticRole` later that some other sem:-based query needs
  won't automatically get a mapping. There's also no corresponding "remove"/"re-sync" step — it
  only ever inserts triples, never revises `ROLE_SUBPROPERTY_MAP` changes already uploaded to an
  older repository.
- **`SaturationTracker`'s targets assume the future looks like the past.** `expected_count`
  (see [Catch-up opening flow](#catch-up-opening-flow-catch_up_from_kgpy)) is a plain historical
  weekly average with no seasonality/trend awareness — a topic the human used to report often but
  has since stopped still gets a target based on their OLD rate, and `MAX_ASKS_PER_TOPIC` (not the
  target) is what actually stops it from being asked about forever in that case.
  `CATCH_UP_WINDOW_DAYS` (7) and `MAX_SATURATION_GAP_DAYS` (14) are both module constants, not
  (yet) per-session parameters — a coach who wants a longer catch-up window, or a baseline measured
  over something other than a week, needs to change `catch_up_from_kg.py` directly.
  `initial_reported_count` is only as complete as `get_temporal_containers()`'s own date-based
  "gap" bucket — an activity with only a vague, unresolved time phrase (`"for an hour"`,
  `"recently"`) never lands there (see `_parse_event_time()`), so pre-existing gap-period data
  like that is invisible to the STARTING seed; live counting via `record_new_activity()` doesn't
  have this problem at all, since it's driven by `on_new_subject` firing on push, not by parsing
  a date back out of the graph afterward.
- **The graph panel's plain-text triples aren't the actual GraphDB Visual graph** — see [Split-
  screen graph panel](#split-screen-graph-panel) for why a real embed isn't practical in Tkinter;
  "Open in GraphDB ↗" is the only way to see the real, interactive view. It also adds one SPARQL
  round-trip per turn (`fetch_triples`) — cheap, but one more thing that can be slow/unreachable
  alongside the OpenAI/gap-lookup calls already on that same background thread. `auto-open new
  activities` defaults off and `self._opened_subject_uris` is a fresh, in-memory, per-window set
  either way, so a closed and reopened window has forgotten what it already auto-opened; turning
  the option back on there can re-open a browser tab for an activity it already opened before.
