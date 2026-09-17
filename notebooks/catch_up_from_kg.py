"""
catch_up_from_kg.py
=====================

The opening, "what happened since we last spoke" phase for kg_catchup_intent_chat.ipynb -- run
ONCE before the live chat starts, on top of src/cltl/gaps_from_kg/get_temporal_containers.py's
brain-side temporal queries (LongTermMemory._submit_query()) -- a different query layer over the
same GraphDB repository than kg_gap_finder.py's rdflib/SPARQL-endpoint queries, which is what the
rest of chat_sessions.py's per-turn intent-driven gap-finding uses.

Flow this module builds:

1. find_last_conversation_date() -- get_temporal_containers.get_last_conversation_date(): the
   most recent date `human` is on record as having spoken in this KG at all (falls back to
   `fallback_date` if the KG has no prior utterance from them yet -- e.g. a brand new human, or a
   fresh/empty graph).
2. find_catch_up_topics() -- for every activity/condition TYPE chat_sessions.py's own per-turn
   intent-driven gap-finding already restricts itself to (DEFAULT_GAP_ACTIVITY_TYPES), run
   get_temporal_containers.get_temporal_containers() once with that type: a type the human has
   real HISTORY with (something dated before the last conversation) is a real catch-up topic --
   sorted most-recently-discussed first.
3. render_opening_question() -- has the LLM phrase ONE natural first turn: names how long it's
   been since the last conversation and invites the human to share what's happened since,
   surfacing a couple of the topics they've talked about before as concrete memory prompts.
4. CatchUpQueue -- the remaining topics (everything render_opening_question() didn't already
   name), handed out one at a time as the conversation's own default reply runs dry -- see
   wrap_agent_fn_with_catch_up().
5. wrap_agent_fn_with_catch_up() -- wraps a plain chat_sessions.openai_agent()-style agent_fn so
   that, for every reply that would otherwise be the DEFAULT LLM fallback (i.e.
   KgChatSession/KgIntentChatSession found no per-turn intent gap to ask about for whatever the
   human just said -- see chat_sessions.KgChatSession.say()'s "default" reply_sources tag), the
   next still-unasked catch-up topic is asked about instead of the plain default reply -- until
   the queue is empty, at which point replies fall back to the wrapped agent_fn unchanged. This
   is the ONLY integration point with chat_sessions.py: nothing in that module needs to change,
   since agent_fn is already a pluggable constructor parameter of every ChatSession.

Once the human mentions an actual NEW activity/condition -- in answer to the opening question, a
catch-up topic, or anything else -- chat_sessions.KgIntentChatSession's own existing per-turn flow
(SRL extraction -> push to the KG -> intent_gap_finder.next_intent_gap()) takes over for it
completely unchanged. This module only ever supplies the OPENING turn and the fallback catch-up
questions asked whenever that per-turn flow itself has nothing to ask.
"""

import sys
from datetime import datetime
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
    from cltl.brain.long_term_memory import LongTermMemory
    _GAPS_DEPS = {
        "get_temporal_containers": get_temporal_containers,
        "LongTermMemory": LongTermMemory,
    }
    return _GAPS_DEPS


def connect_brain(kg_address: str, log_dir: str = "kg_logs"):
    """One LongTermMemory brain connection to `kg_address` -- the exact same address
    chat_sessions.KgChatSession/KgIntentChatSession's own kg_address points
    populate_ekg_from_annotations() at (see events_from_chat/populate_ekg.py), just queried
    through cltl.brain's own SPARQL layer instead of kg_gap_finder.py's rdflib one. Never clears
    the graph (clear_all=False, unconditionally) -- this module only ever reads."""
    deps = _load_gaps_from_kg()
    Path(log_dir).mkdir(parents=True, exist_ok=True)
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


def find_catch_up_topics(brain, current_date: datetime, recent_date: datetime,
                          activity_types=DEFAULT_GAP_ACTIVITY_TYPES) -> List[Dict]:
    """One entry per `activity_types` local name (default: chat_sessions.DEFAULT_GAP_ACTIVITY_TYPES
    -- the same set per-turn intent gap-finding already restricts itself to) the human has real
    HISTORY with in the KG -- i.e. get_temporal_containers.get_temporal_containers()'s own
    "history" bucket (anything dated before `recent_date`, see its docstring) is non-empty for
    that type -- sorted most-recently-discussed first (by each type's own latest history
    activity's own time).

    A type with NO history at all (never discussed, ever) is left out entirely -- there's nothing
    to catch up ON. A type that already has entries in the "gap" bucket (dated between
    `recent_date` and `current_date`, e.g. from data preloaded for this same session) is also left
    out, since asking about it again would be redundant.

    Each entry: {"activity_type", "history_count", "latest_label", "latest_date"} -- the last two
    drawn from the history activity with the most recent "time", for a concrete, natural-sounding
    reference in the LLM-phrased question (see render_opening_question()/CatchUpQueue).
    """
    deps = _load_gaps_from_kg()
    gtc = deps["get_temporal_containers"]
    topics = []
    for activity_type in activity_types:
        history, gap, future, unknown = gtc.get_temporal_containers(
            brain, current_date, recent_date, activity_type="n2mu:" + activity_type
        )
        if not history or gap:
            continue
        latest = max(history, key=lambda a: a["time"])
        topics.append({
            "activity_type": activity_type,
            "history_count": len(history),
            "latest_label": latest["label"],
            "latest_date": latest["time"],
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


def render_opening_question(human: str, current_date: datetime, recent_date: datetime,
                             topics: List[Dict], lead_topics: int = 2,
                             agent_fn=None, model: str = DEFAULT_MODEL) -> str:
    """The very first agent turn: names how long it's been since the last conversation
    (find_last_conversation_date()) and invites `human` to share what's happened since, naming the
    `lead_topics` most-recently-discussed catch-up topics (find_catch_up_topics(), already sorted
    most-recent-first) as concrete memory prompts -- e.g. "how's your exercise routine and your
    sleep been?" -- rather than a bare, generic "what's new?".

    The remaining topics (topics[lead_topics:]) are NOT mentioned here -- see CatchUpQueue, which
    asks about those one at a time as the conversation's own default-reply fallback runs dry.
    """
    lead = topics[:lead_topics]
    topic_phrase = ", ".join(t["activity_type"].replace("_", " ") for t in lead)
    user_prompt = (
        f"Our last conversation was {_format_gap_description(current_date, recent_date)}. "
        + (f"Back then we'd talked about: {topic_phrase}. " if topic_phrase else "")
        + "Write the opening message of today's chat: greet them, mention it's been a while "
        "since we last talked, and ask what's happened since then"
        + (f", specifically inviting them to update you on {topic_phrase}" if topic_phrase else "")
        + ". Keep it natural and short."
    )
    messages = [
        {"role": "system", "content": _catch_up_system_prompt(human)},
        {"role": "user", "content": user_prompt},
    ]
    reply_fn = agent_fn or _default_reply_fn(model)
    return _call_openai("generating the opening catch-up question", reply_fn, messages)


class CatchUpQueue:
    """The catch-up topics (find_catch_up_topics(), minus whichever ones render_opening_question()
    already named) still to ask about -- handed out one at a time, most-recently-discussed first,
    via next_question(), never repeating one already asked. self.asked accumulates every topic
    already handed out, for inspection after the chat. Used by wrap_agent_fn_with_catch_up() as
    the fallback source for every reply that would otherwise be the plain default LLM one."""

    def __init__(self, topics: List[Dict], human: str, model: str = DEFAULT_MODEL):
        self._queue = list(topics)
        self.human = human
        self.model = model
        self.asked = []

    def __len__(self):
        return len(self._queue)

    def next_question(self, agent_fn=None) -> Optional[str]:
        """Pop and ask about the next queued topic, or None once the queue is empty."""
        if not self._queue:
            return None
        topic = self._queue.pop(0)
        self.asked.append(topic)
        label = topic["activity_type"].replace("_", " ")
        user_prompt = (
            f"Last time we spoke, {self.human} mentioned {topic['history_count']} thing(s) "
            f"related to {label} (most recently: \"{topic['latest_label']}\"). Ask them a short, "
            f"natural follow-up question about how their {label} has been since then."
        )
        messages = [
            {"role": "system", "content": _catch_up_system_prompt(self.human)},
            {"role": "user", "content": user_prompt},
        ]
        reply_fn = agent_fn or _default_reply_fn(self.model)
        return _call_openai(f"asking a catch-up question about {label}", reply_fn, messages)


def wrap_agent_fn_with_catch_up(agent_fn, queue: CatchUpQueue):
    """Wrap `agent_fn` (e.g. chat_sessions.openai_agent()) so that every call to it -- which
    chat_sessions.KgChatSession.say() only ever makes once it's found no per-turn intent gap to
    ask about for whatever the human just said, see its own docstring's "default" reply_sources
    tag -- asks the next still-unasked catch-up topic (queue.next_question()) instead, for as long
    as `queue` still has any left; once it's empty, every call goes straight through to `agent_fn`
    unchanged, exactly as if this wrapper wasn't there.
    """
    def _wrapped(messages):
        question = queue.next_question()
        if question is not None:
            return question
        return agent_fn(messages)
    return _wrapped
