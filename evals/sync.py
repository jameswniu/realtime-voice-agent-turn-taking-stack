#!/usr/bin/env python3
"""Port the route probes into ElevenLabs' NATIVE agent test suite.

WHY MOVE THEM. cases.py drives real conversations from this Mac. That
worked, but it is the wrong place for a regression suite:

  - it saturates the mini. Three A/B sweeps died that way -- load 8+, ~100
    openclaw processes, the gateway timing out mid-run.
  - only this machine can run it, so nothing catches a regression unless
    someone remembers to.
  - it cannot gate a change. It reports after the fact.

ElevenLabs runs these on THEIR infrastructure, scoped to a BRANCH, which is the
part that matters: a fix can be tested on a branch before it ever reaches a
caller, and the A/B sweep becomes three branches rather than three hours of
local load.

TWO TEST TYPES, because the probes assert two different things:

  expecting a tool  -> type "tool": tool_call_parameters.referenced_tool.id
                       names the tool that must be invoked.
  expecting NO tool -> type "llm" with a success_condition. A tool test asserts
                       a tool WAS called and cannot express the absence, and the
                       absence is the whole point of the 22 acknowledgement and
                       banter probes ("Thanks, man", "Hmm", "So you are done
                       blonding?"). Those are where she over-calls in production.

KNOWN FIDELITY GAP, stated because it changes what a pass AND A FAILURE mean.

PROVEN 2026-07-27: a failure here can be an artifact. R37 (joke, then "Sure.") failed
this suite 2-4/10 for hours and passed cleanly in the agent's own preview chat, along
with every sibling in the cluster. The reason is structural: a test replays a STATIC
chat_history, so the prior joke is a plain agent message, while in production it arrived
via a tell_joke TOOL CALL and the live state carries that call and its result. She cannot
tell from a transcript that she already ran the tool. Reproduce a single-test failure in
the UI before spending a fix on it; five prompt rewrites went into R37 before anyone
typed it into the product.

The original gap: Locally, a
probe's context turns were REPLAYED against the live agent, so she genuinely
answered them and the follow-up landed in a real conversation. An EL test takes
a STATIC chat_history, and we hold no canned agent replies, so context probes
arrive as consecutive user turns with nothing between. Single-turn probes port
faithfully; multi-turn ones are weaker here than locally. Flagged per test in
the name so a pass is not over-read.

usage:
  sync.py --dry-run          show what would be created
  sync.py --limit 6          pilot a handful first
  sync.py                    create the full suite
  sync.py --purge            delete every ZZ/AJ suite test and stop
"""
import argparse
import contextlib
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.elevenlabs.io/v1/convai"
AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "")
PREFIX = "aj-route"          # every test this script owns starts with this

NO_TOOL_CONDITION = (
    "The agent replies conversationally and calls NO tool at all. "
    "Any tool invocation is a failure, including get_calendar, get_weather, "
    "get_location, get_distance, tell_joke, check_notes, check_telegram, "
    "save_note or calculate. The reply itself may be anything natural: a short "
    "acknowledgement, a direct answer from what it already knows, or a "
    "clarifying question. Only the tool call decides pass or fail."
)


# One success condition per text-graded probe. These are the tests where the
# defect and the fix produce the SAME tool behaviour, so only the words separate
# them.
# Probes that CANNOT be graded on ElevenLabs and stay in cases.py.
# R22's correct reply is four words ("What am I digging into?"), and the judge
# returned "unknown" on all 10 runs with the rationale that the transcript
# "lacks the agent's response" -- while agent_responses plainly held it. Same
# defect R42 shows intermittently, and it tracks reply LENGTH, not content. A
# test that cannot return a verdict is worse than no test here: it reads as a
# failure to anything scoring on status alone. The local harness grades this one
# with a regex validated against her real bad replies, no judge involved.
NOT_PORTABLE = {"R22": "judge returns unknown on very short replies"}

TEXT_CONDITIONS = {
    # R20: someone else is in the room and has heard nothing.
    #
    # CALLING tell_joke COUNTS AS TELLING ONE. It has to: a tool call ends the
    # run in this harness, so the joke text never materialises, and an earlier
    # version of this condition left the judge to guess. It guessed both ways on
    # IDENTICAL transcripts -- "successfully invoked tell_joke" and "failed to
    # tell a joke, instead opting to call a tool" -- which made R20 flip between
    # 3/10 and 6/10 on an unchanged agent and nearly got a good prompt change
    # reverted as a regression.
    # SEMANTICS FLIPPED 2026-08-02 (the user). This condition used to prefer a
    # FRESH joke and fail a verbatim repeat -- and on the 08:48 run she did
    # exactly what it asked, told the friend a brand-new joke, and the user called
    # it wrong: "tell my gym friend THE joke" means relay the one he just
    # heard. The old loose form also let the structural judge pass ANY joke,
    # which is why only the condition-blind vibes judge caught the miss (the
    # R20 flag). The judge can now verify "same joke" because the runner feeds
    # her context-turn replies into harness_facts verbatim.
    "R20": (
        "The user just heard a joke (it appears in the harness facts as what the "
        "agent said EARLIER in this conversation). His gym friend has heard "
        "nothing, and 'the joke' means THAT SAME joke. "
        "PASS only if the agent's new turn says that same joke again -- same "
        "premise and same punchline; smoothing a word or two is fine. "
        "FAIL if the agent (a) declines, or says it already told it or that he "
        "has already heard it, (b) tells a DIFFERENT joke than the earlier one, "
        "or (c) calls the tell_joke tool -- per the harness facts a tool call "
        "fetches a NEW joke, and 'the joke' is not a new one."),
    # R22: "AJ" is the agent's OWN name. Asking what to research is fine;
    # handing the task to a third person is not.
    "R22": (
        "PASS if the agent responds in the FIRST PERSON, as the one who will do "
        "the research itself -- either asking what to look into, or starting the "
        "lookup. "
        "FAIL if it treats AJ as a separate person or system: saying it "
        "will ask AJ, asking what it should ask him, referring to AJ "
        "as he/him, or offering to relay the request. AJ is the agent's own "
        "name. Whether a tool is called is irrelevant here."),
}

# Context turns that were ANSWERED BY A TOOL in real life. Flattening those into a
# plain agent message is what made R37 lie: the suite failed it 0/5 while the
# product passed it 4/4, because from a flat transcript she cannot tell she already
# ran tell_joke. Measured 2026-07-27 on R37, same behaviour asked four ways:
#
#     0/5   flat history (what this script used to emit)
#     2/5   + the tool call and its result as real turns
#     3/5   + dynamic variables resolved
#     4/4   the product itself
#
# Each element recovers part of the gap and none of them closes it, so a failure
# here is still a hypothesis to reproduce in the preview chat -- just far less
# often than before.
# Recorded fixtures beat reconstruction. Measured on R37, same behaviour, five ways:
#   0/5 flat | 0/5 recorded verbatim | 2/5 recorded w/ greeting | 4/5 reconstructed
#   5/5 RECORDED, greeting + metadata stripped | 4/4 the product
# Capture fixtures by driving the utterance through the preview chat and
# recording the transcript verbatim. Falls back to reconstruction when no fixture exists.
FIXTURES = (os.environ.get("AJ_OUT") or os.path.expanduser("~/.aj-voice-agent")) + "/fixtures"


def _fixture(utterance):
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", utterance.lower()).strip("-")[:60]
    path = os.path.join(FIXTURES, slug + ".json")
    if os.path.isfile(path):
        with open(path) as fh:
            return json.load(fh)
    return None


CTX_TOOLS = {
    "tell me a joke.": "tell_joke",
    "hey, tell my gym friend a joke.": "tell_joke",
    "what events do i have on my calendar?": "get_calendar",
    "what events do i have this week?": "get_calendar",
    "how far away is the ferry building?": "get_distance",
}

# Unresolved {{call_reason}} leaves her prompt saying the call was scheduled for
# "{{call_reason}}", which reads as licence to keep doing whatever the context was.
# Pinned to the value a normal inbound call carries.
DYNAMIC_VARS = {"call_reason": "no reason given"}

def key():
    return os.environ["ELEVENLABS_API_KEY"]


def call(method, path, body=None):
    req = urllib.request.Request(
        f"{API}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}


def load_probes():
    spec = importlib.util.spec_from_file_location(
        "rp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.py"))
    m = importlib.util.module_from_spec(spec)
    sys.argv = ["x", "--list"]          # module parses argv at import
    # The probe module calls sys.exit() after --list; that is expected, not a
    # failure, and we only want its PROBES table.
    with contextlib.suppress(SystemExit):
        spec.loader.exec_module(m)
    return [p for p in m.PROBES if p[1] == "READ"], getattr(m, "TEXT_RULES", {})


def recorded_replies():
    """Her ACTUAL replies to each probe utterance, from the local sweep results.

    Without these a context probe becomes two consecutive user turns and the
    agent never receives the answer the follow-up depends on. R12 ("how long by
    car?") is the clean example: locally it passes because she had already said
    "two point seven miles, eight minutes by car"; as bare user turns she has
    been told nothing, calls get_distance, and the test fails for a reason that
    has nothing to do with routing.
    """
    import glob
    out = {}
    base = os.environ.get("AJ_OUT") or os.path.expanduser("~/.aj-voice-agent/route-probe")
    files = sorted(glob.glob(base + "/route-*.json"), key=os.path.getmtime)
    for f in files:                      # newest wins
        if os.path.getsize(f) < 4000:
            continue
        try:
            with open(f) as fh:
                for r in json.load(fh)["results"]:
                    if r.get("reply") and r.get("correct"):
                        out[r["utterance"]] = r["reply"]
        except (OSError, ValueError, KeyError):
            continue
    # Five context strings are paraphrases that were never probes themselves, so
    # no sweep ever recorded a reply for them. They were asked once directly and
    # cached here; regenerate with sync.py --capture-context if the
    # agent changes enough that the old answers stop being representative.
    cache = (os.environ.get("AJ_OUT") or os.path.expanduser("~/.aj-voice-agent/route-probe")) + "/context-replies.json"
    if os.path.isfile(cache):
        try:
            with open(cache) as fh:
                out.update(json.load(fh))
        except (OSError, ValueError):
            pass
    return out


REPLIES = recorded_replies()


def to_test(pid, exp, ctx, utt, text_ruled):
    """One probe -> one EL test payload."""
    history = []
    for c in ctx:
        rec = _fixture(c)
        if rec:
            # A real recording of this exact exchange, tool turns and all.
            history.extend(rec)
            continue
        history.append({"role": "user", "message": c, "time_in_call_secs": 0})
        # Insert her real recorded answer so the follow-up lands in a
        # conversation that actually happened, not a monologue.
        if c in REPLIES:
            tool = CTX_TOOLS.get(c.strip().lower())
            if tool:
                # Rebuild the TOOL turns too, not just the words. The result_value
                # shape is the shim's ({"answer": ...}); what matters to the agent
                # is that a call happened and returned, which a flat message cannot
                # convey.
                rid = f"{tool}_ctx"
                history.append({"role": "agent", "message": "", "time_in_call_secs": 0,
                                "tool_calls": [{"type": "webhook", "request_id": rid,
                                                "tool_name": tool, "params_as_json": "{}",
                                                "tool_has_been_called": True}]})
                history.append({"role": "agent", "message": "", "time_in_call_secs": 0,
                                "tool_results": [{"request_id": rid, "tool_name": tool,
                                                  "result_value": json.dumps({"answer": REPLIES[c]}),
                                                  "is_error": False, "type": "webhook",
                                                  "tool_has_been_called": True}]})
            history.append({"role": "agent", "message": REPLIES[c],
                            "time_in_call_secs": 0})
    history.append({"role": "user", "message": utt, "time_in_call_secs": 0})
    # [ctx] marks a multi-turn test. It now carries her recorded replies, so it
    # is faithful; [ctx!] means a reply was missing and the turn is a bare user
    # message, which is weaker than the local run and should not be read as a
    # clean pass.
    missing = [c for c in ctx if c not in REPLIES]
    tag = f"{PREFIX} {pid}" + (" [ctx!]" if missing else " [ctx]" if ctx else "")
    if text_ruled:
        # graded on what she SAYS, not which tool -- the local harness had to do
        # the same for R20, where the bug and the fix both call zero tools.
        return {"name": f"{tag} [text]", "type": "llm", "chat_history": history,
                "dynamic_variables": DYNAMIC_VARS,
                "success_condition": TEXT_CONDITIONS[pid]}
    if exp:
        return {"name": tag, "type": "tool", "chat_history": history,
                "dynamic_variables": DYNAMIC_VARS,
                "tool_call_parameters": {
                    "referenced_tool": {"id": exp, "type": "webhook"}}}
    return {"name": f"{tag} [no-tool]", "type": "llm", "chat_history": history,
            "dynamic_variables": DYNAMIC_VARS,
            "success_condition": NO_TOOL_CONDITION}


def existing():
    out, cursor = {}, None
    while True:
        path = "agent-testing" + (f"?cursor={cursor}" if cursor else "")
        d = call("GET", path)
        for t in d.get("tests", []):
            out[t.get("name", "")] = t.get("id")
        if not d.get("has_more"):
            return out
        cursor = d.get("next_cursor")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--purge", action="store_true")
    a = ap.parse_args()

    have = existing()
    if a.purge:
        n = 0
        for name, tid in have.items():
            if name.startswith((PREFIX, "ZZ-")):
                call("DELETE", f"agent-testing/{tid}")
                n += 1
        print(f"purged {n} tests")
        return 0

    probes, text_rules = load_probes()
    if a.limit:
        # pilot on a mix, not just the first N of one kind
        with_tool = [p for p in probes if p[2]][: max(1, a.limit // 2)]
        no_tool = [p for p in probes if not p[2]][: a.limit - len(with_tool)]
        probes = with_tool + no_tool

    made = skipped = failed = 0
    for pid, _safety, exp, ctx, utt in probes:
        if pid in NOT_PORTABLE:
            print(f"  skipping {pid}: {NOT_PORTABLE[pid]} (stays local)")
            continue
        payload = to_test(pid, exp, ctx, utt, pid in text_rules)
        if payload["name"] in have:
            skipped += 1
            continue
        kind = payload["type"] + ("/" + exp if exp else "/none")
        if a.dry_run:
            print(f"  WOULD CREATE {payload['name']:24} {kind:22} "
                  f"{len(payload['chat_history'])} turn(s)  {utt[:44]}")
            made += 1
            continue
        r = call("POST", "agent-testing/create", payload)
        if "_error" in r:
            print(f"  FAIL {payload['name']}: {r['_error']} {r['_body'][:120]}")
            failed += 1
        else:
            print(f"  created {payload['name']:24} {kind:22} {r['id']}")
            made += 1

    print(f"\n{'would create' if a.dry_run else 'created'}: {made}   "
          f"already present: {skipped}   failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
