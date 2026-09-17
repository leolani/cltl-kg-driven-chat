"""
catch_up_from_kg.py
=====================

The "what happened since we last spoke" phase for kg_catchup_intent_chat.ipynb, built on top of
src/cltl/gaps_from_kg/get_temporal_containers.py's brain-side temporal queries
(LongTermMemory._submit_query()) -- a different query layer over the same GraphDB repository than
kg_gap_finder.py's rdflib/SPARQL-endpoint queries, which is what the rest of chat_sessions.py's
per-turn intent-driven gap-finding uses.

This isn't just a one-shot opening line: it drives the WHOLE conversation, in a loop, until this
human's gap period (the time between find_last_conversation_date() and "now") has as much
knowledge behind it as their own history says is typical -- not "ask once per topic and move on
regardless." The loop:

  1. Ask about a gap-period topic (SaturationTracker.next_question()) -- one of
     DEFAULT_GAP_ACTIVITY_TYPES the human has real history with but not yet ENOUGH reported for
     this gap period (see "Saturation", below).
  2. Whatever activity/condition the human reports in reply is handled entirely by
     chat_sessions.KgIntentChatSession's own EXISTING per-turn flow, completely unchanged: SRL
     extraction -> push to the KG -> intent_gap_finder.next_intent_gap() keeps asking follow-up
     questions about THAT SAME activity (what/how much/when/where) for as long as its own matching
     intent still has unmet requirements.
  3. Once that activity's own follow-ups are exhausted (say() falls through to the default
     agent_fn reply -- see chat_sessions.KgChatSession.say()'s "default" reply_sources tag), go
     back to step 1 -- another gap-period topic still short of saturation, or the SAME one again
     if it still is -- unless every topic has reached saturation, in which case replies fall
     through to the wrapped agent_fn unchanged and the conversation continues normally.

Saturation -- "enough knowledge for the gap period", this module's whole goal -- is defined per
topic as the AVERAGE FREQUENCY of that topic in periods the same length as the gap, found by
tiling that same window size backwards across the human's own history before the gap even started
(_windowed_average_rate()): if they've historically reported "exercise" an average of 3 times per
week-long period, and the gap is a week, 3 exercise activities reported live this session is
"enough" -- not "however many kg_gap_finder.py or intent_gap_finder.py happen to ask about", and
not "exactly 1, regardless of how often this person usually reports it."

Module contents:

- connect_brain()/ensure_role_hierarchy() -- see their own docstrings: makes sure
  n2mu_sem_roles.py's rdfs:subPropertyOf mapping is uploaded, without which
  get_temporal_containers() finds no date/actor/place for any real activity at all.
- find_last_conversation_date() -- get_temporal_containers.get_last_conversation_date().
- find_catch_up_topics() -- one entry per DEFAULT_GAP_ACTIVITY_TYPES type with real history,
  carrying both a concrete example (latest_label/latest_date) and its own saturation target
  (expected_count, from _windowed_average_rate()) and however much of it the KG's own "gap"
  bucket already covers before this conversation even starts (initial_reported_count).
- SaturationTracker -- holds those targets plus how many of each topic have actually been
  reported LIVE this session (self.reported, updated via record_new_activity()), decides what's
  still worth asking about (next_question()) and when the whole loop is done (is_saturated()).
  Also builds the very first turn (opening_question()).
- wrap_agent_fn_with_saturation_loop() -- the ONLY integration point with chat_sessions.py: wraps
  a plain agent_fn so a still-unsaturated topic's question is asked instead of the plain default
  reply, until SaturationTracker.is_saturated(). Nothing in chat_sessions.py needs to change for
  THIS -- agent_fn is already a pluggable constructor parameter of every ChatSession -- but
  SaturationTracker.record_new_activity() needs to be told about every new activity as it's
  pushed, which DOES need one small, additive hook: chat_sessions.KgChatSession's own
  `on_new_subject` constructor parameter (see its docstring), called exactly once per genuinely
  NEW activity_id (never on a later turn that just adds another role to one already known).
"""

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from chat_sessions import DEFAULT_GAP_ACTIVITY_TYPES, DEFAULT_MODEL, _call_openai, _openai_client


def _find_repo_src_dir() -> Path:
    """The repo's own src/ directory (parent of the cltl/ namespace package), found by walking up
    from the current working directory -- added to sys.path so `import
    cltl.gaps_from_kg.get_temporal_containers` resolves via Python's namespace-package merging:
    cltl/ itself has no __init__.py here (it's shared with the separately pip-installed
    cltl.brain/cltl.commons/... packages), and PEP 420 merges every "cltl" directory found on
    sys.path into that one namespace, so adding src/ is enough -- no need to import gaps_from_kg's
    own directory directly the way chat_sessions._load_kg_dependencies() does for
    chat_from_kg/events_from_chat's bare-name-importing flat modules."""
    for base in (Path.cwd(), *Path.cwd().parents):
        candidate = base / "src"
        if (candidate / "cltl" / "gaps_from_kg").is_dir():
            return candidate
    raise FileNotFoundError(
        "Couldn't locate src/cltl/gaps_from_kg from the current working directory "
        f"({Path.cwd()}); run this notebook from within the kg-chat repo."
    )


_GAPS_DEPS = None


def _load_gaps_from_kg():
    """Import get_temporal_containers and LongTermMemory on first use only -- mirrors
    chat_sessions._load_kg_dependencies()'s own "don't pay for cltl.brain/rdflib unless this
    feature is actually used" reasoning."""
    global _GAPS_DEPS
    if _GAPS_DEPS is not None:
        return _GAPS_DEPS
    src_dir = _find_repo_src_dir()
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import cltl.gaps_from_kg.get_temporal_containers as get_temporal_containers
    import cltl.gaps_from_kg.n2mu_sem_roles as n2mu_sem_roles
    from cltl.brain.long_term_memory import LongTermMemory
    _GAPS_DEPS = {
        "get_temporal_containers": get_temporal_containers,
        "n2mu_sem_roles": n2mu_sem_roles,
        "LongTermMemory": LongTermMemory,
    }
    return _GAPS_DEPS


def ensure_role_hierarchy(kg_address: str) -> bool:
    """Make sure `kg_address` has the n2mu_sem_roles.py role mappings uploaded -- see that
    module's own docstring for why get_temporal_containers()/get_last_conversation_date() find
    nothing at all without them (thought_util.get_sem_relation_query() only ever sees an
    activity's actor/place/time through those `rdfs:subPropertyOf` triples). A no-op if they're
    already there (n2mu_sem_roles.role_hierarchy_uploaded()). Returns True if an upload actually
    happened, False if the mapping was already present."""
    deps = _load_gaps_from_kg()
    roles = deps["n2mu_sem_roles"]
    if roles.role_hierarchy_uploaded(kg_address):
        return False
    roles.upload_role_hierarchy(kg_address)
    return True


def connect_brain(kg_address: str, log_dir: str = "kg_logs"):
    """One LongTermMemory brain connection to `kg_address` -- the exact same address
    chat_sessions.KgChatSession/KgIntentChatSession's own kg_address points
    populate_ekg_from_annotations() at (see events_from_chat/populate_ekg.py), just queried
    through cltl.brain's own SPARQL layer instead of kg_gap_finder.py's rdflib one. Never clears
    the graph (clear_all=False, unconditionally) -- this module only ever reads.

    Also calls ensure_role_hierarchy() -- every caller of connect_brain() goes on to run
    find_last_conversation_date()/find_catch_up_topics(), both built on
    thought_util.get_sem_relation_query(), so there's no point ever connecting without it."""
    deps = _load_gaps_from_kg()
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    ensure_role_hierarchy(kg_address)
    return deps["LongTermMemory"](address=kg_address, log_dir=Path(log_dir), clear_all=False)


def find_last_conversation_date(human: str, brain, current_date: datetime,
                                 fallback_date: datetime) -> datetime:
    """The most recent date `human` is on record as having spoken in this KG at all, via
    get_temporal_containers.get_last_conversation_date() -- `fallback_date` is used as-is when the
    KG has no prior utterance from them (e.g. a brand new human, or a fresh/empty graph)."""
    deps = _load_gaps_from_kg()
    return deps["get_temporal_containers"].get_last_conversation_date(
        human, brain, current_date, fallback_date
    )


def _windowed_average_rate(history_dates: List[datetime], window_days: int,
                            series_end: datetime) -> float:
    """The average number of `history_dates` per non-overlapping `window_days`-long window,
    tiling BACKWARDS from `series_end` (recent_date -- the gap's own start) through the earliest
    of `history_dates` -- "the average frequency of this topic in periods the same length as the
    gap, before the gap", per this module's own saturation goal (see the module docstring).

    A history spanning less than one full window still counts as exactly one (mostly-empty)
    window rather than being skipped or extrapolated -- an infrequent topic's genuinely low rate
    is real information (e.g. "this person mentions treatment once every couple of months"), not
    something to inflate by only counting "full" windows. Returns 0.0 for no history at all (the
    caller -- find_catch_up_topics() -- never calls this for a topic with none anyway).
    """
    if not history_dates:
        return 0.0
    window = timedelta(days=max(window_days, 1))
    earliest = min(history_dates)
    span = series_end - earliest
    num_windows = max(1, math.ceil(span / window)) if span > timedelta(0) else 1
    counts = []
    window_end = series_end
    for _ in range(num_windows):
        window_start = window_end - window
        counts.append(sum(1 for d in history_dates if window_start <= d < window_end))
        window_end = window_start
    return sum(counts) / len(counts)


def find_catch_up_topics(brain, current_date: datetime, recent_date: datetime,
                          activity_types=DEFAULT_GAP_ACTIVITY_TYPES) -> List[Dict]:
    """One entry per `activity_types` local name (default: chat_sessions.DEFAULT_GAP_ACTIVITY_TYPES
    -- the same set per-turn intent gap-finding already restricts itself to) the human has real
    HISTORY with in the KG -- i.e. get_temporal_containers.get_temporal_containers()'s own
    "history" bucket (anything dated before `recent_date`, see its docstring) is non-empty for
    that type -- sorted most-recently-discussed first (by each type's own latest history
    activity's own time). A type with NO history at all is left out entirely -- there's nothing to
    calibrate a saturation target against, let alone catch up on.

    Each entry: {"activity_type", "history_count", "latest_label", "latest_date",
    "expected_count", "initial_reported_count"}:

    - "expected_count" (see _windowed_average_rate()) -- how many of this topic's activities this
      human would TYPICALLY report over a period as long as the current gap
      (current_date - recent_date), based on the average across every gap-length window found
      tiling backwards through their own history before `recent_date`. At least 1 whenever there's
      any history at all, so even an infrequent topic still gets asked about once -- this is
      SaturationTracker's own per-topic target.
    - "initial_reported_count" -- however many of this topic's activities are ALREADY in the KG's
      own "gap" bucket (dated between `recent_date` and `current_date`) before this conversation
      even starts, e.g. from data pushed through some other channel. Seeded into
      SaturationTracker.reported so a topic that's already partly (or fully) covered needs that
      much LESS asked about live, instead of double-counting it.
    """
    deps = _load_gaps_from_kg()
    gtc = deps["get_temporal_containers"]
    gap_days = max((current_date.date() - recent_date.date()).days, 1)
    topics = []
    for activity_type in activity_types:
        history, gap, future, unknown = gtc.get_temporal_containers(
            brain, current_date, recent_date, activity_type="n2mu:" + activity_type
        )
        if not history:
            continue
        latest = max(history, key=lambda a: a["time"])
        rate = _windowed_average_rate([a["time"] for a in history], gap_days, recent_date)
        topics.append({
            "activity_type": activity_type,
            "history_count": len(history),
            "latest_label": latest["label"],
            "latest_date": latest["time"],
            "expected_count": max(1, round(rate)),
            "initial_reported_count": len(gap),
        })
    topics.sort(key=lambda t: t["latest_date"], reverse=True)
    return topics


def _catch_up_system_prompt(human: str) -> str:
    """System prompt for every LLM call this module makes -- same framing as
    chat_sessions.default_system_prompt(), restrained to short, non-advice-giving replies, since
    these are all opening/catch-up QUESTIONS, not coaching."""
    return (
        f"You are a lifestyle coach talking with {human}, a person with Type 2 diabetes, at the "
        "start of a new chat conversation about diet, exercise, sleep, stress and daily routines "
        "that affect their blood sugar management. Keep your reply short (1-3 sentences) and "
        "warm. Only ever ask a question -- do not give advice, recommendations, or suggestions."
    )


def _format_gap_description(current_date: datetime, recent_date: datetime) -> str:
    """A human-ish phrase for how long it's been since `recent_date` -- "yesterday", "3 days
    ago, on Tuesday", etc. -- for the LLM prompts below."""
    days = (current_date.date() - recent_date.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago, on {recent_date.strftime('%A, %B %d')}"


def _default_reply_fn(model: str):
    return lambda messages: _openai_client().chat.completions.create(
        model=model, messages=messages
    ).choices[0].message.content


class SaturationTracker:
    """Drives the "keep asking about gap-period topics until we have enough" loop (see this
    module's own docstring). Holds each catch-up topic's saturation TARGET
    (find_catch_up_topics()'s own "expected_count", derived from how often this human
    historically reported this topic in gap-length periods before now) alongside how many of
    that topic's activities have actually been reported so far THIS session (self.reported,
    seeded from "initial_reported_count" and updated live via record_new_activity() -- see
    chat_sessions.KgChatSession's own `on_new_subject` hook, the only integration point this
    needs with chat_sessions.py beyond the plain agent_fn wrapping).

    next_question() asks about whichever still-unsaturated topic has the BIGGEST shortfall
    (expected_count - reported), tie-broken by most-recently-discussed -- phrasing a follow-up
    ("anything else...") differently once a topic's already been asked about before this session
    (self.asked). Gives up on a topic once it's been asked MAX_ASKS_PER_TOPIC times regardless of
    whether its target was ever reached -- the same backstop philosophy as
    chat_sessions.KgIntentChatSession's own MAX_INTENT_GAP_ATTEMPTS: a human who simply has
    nothing more to say about a topic shouldn't be asked about it forever. Since every topic is
    capped this way, the WHOLE loop is bounded too (at most
    len(topics) * MAX_ASKS_PER_TOPIC catch-up questions, even in the worst case).

    is_saturated() is True once every target topic is either at/above its own expected_count or
    has hit that per-topic ask cap -- "enough knowledge for the gap period" (this module's own
    stated goal), not "literally every topic's exact target hit no matter what."
    """

    MAX_ASKS_PER_TOPIC = 3

    def __init__(self, topics: List[Dict], human: str, model: str = DEFAULT_MODEL):
        self.targets: Dict[str, Dict] = {t["activity_type"]: t for t in topics}
        self.reported: Dict[str, int] = {
            t: target["initial_reported_count"] for t, target in self.targets.items()
        }
        self.asked: Dict[str, int] = {t: 0 for t in self.targets}
        self.human = human
        self.model = model
        # [{"activity_type", "attempt"}, ...] -- one entry per next_question() call, for
        # inspection after the chat (chat_sessions.py's own turn_log has no notion of this
        # module's questions -- they're plain "default" reply_sources turns from its own point of
        # view, indistinguishable from a generic LLM reply without this).
        self.asked_log: List[Dict] = []

    def record_new_activity(self, activity_type: str) -> None:
        """Call whenever a NEW activity/condition of `activity_type` is pushed to the KG during
        this live session (see chat_sessions.KgChatSession's `on_new_subject` hook) -- a no-op for
        any type this tracker isn't targeting (not one of find_catch_up_topics()'s own topics)."""
        if activity_type in self.reported:
            self.reported[activity_type] += 1

    def _remaining(self) -> List[str]:
        """Topics still worth asking about: short of their own target AND not yet at the
        per-topic ask cap."""
        return [
            t for t, target in self.targets.items()
            if self.reported[t] < target["expected_count"] and self.asked[t] < self.MAX_ASKS_PER_TOPIC
        ]

    def is_saturated(self) -> bool:
        """True once there's nothing left worth asking about -- see _remaining()."""
        return not self._remaining()

    def _mark_asked(self, activity_type: str) -> None:
        self.asked[activity_type] += 1
        self.asked_log.append({"activity_type": activity_type, "attempt": self.asked[activity_type]})

    def opening_question(self, current_date: datetime, recent_date: datetime, lead_topics: int = 2,
                          agent_fn=None) -> str:
        """The very first agent turn: names how long it's been since the last conversation and
        invites `self.human` to share what's happened since, naming the `lead_topics` topics with
        the biggest shortfall (see _remaining()'s own ordering) as concrete memory prompts -- e.g.
        "how's your exercise routine and your sleep been?" -- rather than a bare, generic "what's
        new?". Marks those `lead_topics` topics as asked once (_mark_asked()) so next_question()
        doesn't immediately ask about them again right after the opening line already did.
        """
        remaining = sorted(
            self._remaining(),
            key=lambda t: (
                -(self.targets[t]["expected_count"] - self.reported[t]),
                -self.targets[t]["latest_date"].timestamp(),
            ),
        )
        lead = remaining[:lead_topics]
        for activity_type in lead:
            self._mark_asked(activity_type)
        topic_phrase = ", ".join(t.replace("_", " ") for t in lead)
        user_prompt = (
            f"Our last conversation was {_format_gap_description(current_date, recent_date)}. "
            + (f"Back then we'd talked about: {topic_phrase}. " if topic_phrase else "")
            + "Write the opening message of today's chat: greet them, mention it's been a while "
            "since we last talked, and ask what's happened since then"
            + (f", specifically inviting them to update you on {topic_phrase}" if topic_phrase else "")
            + ". Keep it natural and short."
        )
        messages = [
            {"role": "system", "content": _catch_up_system_prompt(self.human)},
            {"role": "user", "content": user_prompt},
        ]
        reply_fn = agent_fn or _default_reply_fn(self.model)
        return _call_openai("generating the opening catch-up question", reply_fn, messages)

    def next_question(self, agent_fn=None) -> Optional[str]:
        """Ask about whichever still-unsaturated topic (see _remaining()) has the biggest
        shortfall (expected_count - reported so far), tie-broken by most-recently-discussed --
        or None once every topic is saturated or capped out. Phrases a FOLLOW-UP ("anything
        else...") when this topic's already been asked about before this session, instead of
        repeating the exact same question."""
        remaining = self._remaining()
        if not remaining:
            return None
        remaining.sort(key=lambda t: (
            -(self.targets[t]["expected_count"] - self.reported[t]),
            -self.targets[t]["latest_date"].timestamp(),
        ))
        activity_type = remaining[0]
        target = self.targets[activity_type]
        is_followup = self.asked[activity_type] > 0
        self._mark_asked(activity_type)
        label = activity_type.replace("_", " ")
        if is_followup:
            user_prompt = (
                f"Earlier {self.human} mentioned {label} (most recently: "
                f"\"{target['latest_label']}\"), but that's still fewer than what's typical for "
                f"them over a period like this. Ask a short, natural follow-up inviting them to "
                f"share ANOTHER {label}-related thing from the same period, without repeating the "
                f"exact same question as before."
            )
        else:
            user_prompt = (
                f"Historically {self.human} has reported about {label} roughly "
                f"{target['expected_count']} time(s) over a period this length (most recently: "
                f"\"{target['latest_label']}\"). Ask them a short, natural question inviting them "
                f"to share what's happened with their {label} during this period."
            )
        messages = [
            {"role": "system", "content": _catch_up_system_prompt(self.human)},
            {"role": "user", "content": user_prompt},
        ]
        reply_fn = agent_fn or _default_reply_fn(self.model)
        return _call_openai(f"asking a catch-up question about {label}", reply_fn, messages)


def wrap_agent_fn_with_saturation_loop(agent_fn, tracker: SaturationTracker):
    """Wrap `agent_fn` (e.g. chat_sessions.openai_agent()) so that every call to it -- which
    chat_sessions.KgChatSession.say() only ever makes once it's found no per-turn intent gap left
    to ask about for whatever the human just said, see its own docstring's "default"
    reply_sources tag -- asks about the next still-unsaturated gap-period topic
    (tracker.next_question()) instead of the plain default reply, for as long as
    `tracker.is_saturated()` is False; once every topic is saturated (or capped out), every call
    goes straight through to `agent_fn` unchanged, exactly as if this wrapper wasn't there.
    """
    def _wrapped(messages):
        if not tracker.is_saturated():
            question = tracker.next_question()
            if question is not None:
                return question
        return agent_fn(messages)
    return _wrapped
