#!/usr/bin/env python3
"""Tier-1 deterministic grader for the DIY AJ regression harness.

WHY THIS EXISTS. ElevenLabs' suite costs ~60-70k credits a pull and grades our
tests with an undisclosed judge we cannot configure. Mining their own 275
rationales (2026-07-29 baseline) showed the judged layer IS the suite's noise
floor: it overrode a written condition on R37 ("Only the tool call decides pass
or fail" -- it failed a no-tool run anyway on an invented completeness rule),
narrated a "fresh joke" that never existed in the transcript, and silently
softened "any tool" into "any unauthorized tool". Meanwhile their TOOL tests
were never LLM-judged at all (boilerplate "Expected tool called" gives it away).

So 54 of our 55 tests need no judge. This file is that grader. It is pure code:
no model, no credits, no nondeterminism.

VALIDATION MODE. Run with --replay to grade ElevenLabs' own stored corpus and
diff against their verdicts. That is how this file earns trust before it gates
anything: agreement everywhere except the runs where their judge was wrong.

    grade.py --replay <invocation.json>
"""
import argparse
import json
import re
import sys

# Tools the agent uses to answer the user. A test's expectations are about these.
BUSINESS_TOOLS = {
    "tell_joke", "get_calendar", "check_notes", "get_weather", "get_location",
    "get_distance", "calculate", "check_telegram", "list_calls", "cancel_call",
    "schedule_call", "create_playlist", "play_music", "save_note", "set_volume",
    "send_telegram", "get_localtime",
}

# CALL-CONTROL TOOLS ARE NOT BUSINESS TOOLS (red-team finding, 2026-07-30).
# The live agent also carries end_call / skip_turn / voicemail_detection /
# language_detection. A no-tool test asserts "she answered without looking
# anything up" -- it never meant "she may not hang up". Counting these would
# turn a legitimate goodbye into a deterministic fail, amplified x5 by repeats.
CALL_CONTROL_TOOLS = {
    "end_call", "skip_turn", "voicemail_detection", "language_detection",
    "transfer_to_agent", "transfer_to_number", "play_keypad_touch_tone",
}


def new_business_calls(agent_responses):
    """Business tool calls the agent made in the graded turns.

    Only calls that ACTUALLY FIRED count: a [ctx] test replays history that
    already contains tool turns, and seeded history is not the agent's doing.
    """
    calls = []
    for turn in agent_responses or []:
        for tc in turn.get("tool_calls") or []:
            name = tc.get("tool_name")
            if not tc.get("tool_has_been_called"):
                continue
            if name in CALL_CONTROL_TOOLS:
                continue
            if name in BUSINESS_TOOLS or name not in CALL_CONTROL_TOOLS:
                calls.append(name)
    return calls


def has_content(agent_responses):
    """Did the agent actually take a turn?

    A TOOL CALL IS A COMPLETE TURN (red-team finding). The first draft of the
    integrity gate demanded a non-empty reply, which would have ERRORed all five
    of R20's genuinely passing runs -- they end at the tell_joke call with no
    spoken text. Requiring words there would block the exact branch tier 1 passes.
    """
    for turn in agent_responses or []:
        if (turn.get("message") or "").strip():
            return True
        if turn.get("tool_calls"):
            return True
    return False


def grade(expected_tool, agent_responses):
    """Grade one run. Returns (verdict, detail).

    verdict: 'pass' | 'fail' | 'error' | 'undecided'
      error     -- harness/instrument problem, never an agent failure
      undecided -- genuinely semantic, hand to the tier-2 judge
    """
    if not has_content(agent_responses):
        return "error", "no agent turn captured (harness defect, not an agent fail)"

    # SHAPE E: the probe FORBIDS a tool ("!end_call"). Written for the rule that
    # "'THANKS' NEVER ENDS A CALL, however many times he says it" -- the user would
    # "rather be asked twice than hung up on once", and the transcriber confuses
    # 'next' with 'thanks', so hanging up on a thanks can cut him off mid-call.
    # Without this the failure is INVISIBLE: end_call is filtered out of business
    # calls (so a legitimate goodbye cannot fail a no-tool test), which means a
    # premature hangup silently PASSED every existing probe. A wrong hangup is
    # the irreversible failure in his annoying-over-irreversible doctrine, so it
    # gets an explicit assertion rather than riding on a filter.
    if isinstance(expected_tool, str) and expected_tool.startswith("!"):
        forbidden = expected_tool[1:]
        fired = [tc.get("tool_name")
                 for turn in agent_responses or []
                 for tc in (turn.get("tool_calls") or [])
                 if tc.get("tool_has_been_called")]
        if forbidden in fired:
            return "fail", f"agent called {forbidden}, which this test forbids"
        return "pass", f"{forbidden} correctly NOT called"

    # SHAPE D: the probe expects a CALL-CONTROL tool (end_call). Those are
    # filtered out of new_business_calls by design -- a legitimate goodbye must
    # never fail a no-tool test -- but that same filter made a missing hangup
    # invisible to grading. When a probe names one explicitly, assert it FIRED
    # (the user, 2026-07-30: "a pass without hangup = suite miss").
    if expected_tool in CALL_CONTROL_TOOLS:
        fired = [tc.get("tool_name")
                 for turn in agent_responses or []
                 for tc in (turn.get("tool_calls") or [])
                 if tc.get("tool_has_been_called")]
        if expected_tool in fired:
            return "pass", f"call-control tool fired ({expected_tool})"
        return "fail", f"expected {expected_tool}, agent called {fired or 'nothing'}"

    calls = new_business_calls(agent_responses)

    if expected_tool is None:                      # shape B: no tool at all
        if not calls:
            return "pass", "no business tool called"
        return "fail", f"expected no tool, agent called {calls}"

    if len(calls) != 1:                            # shape A: exactly this tool
        return "fail", f"expected exactly 1 call to {expected_tool}, got {calls}"
    if calls[0] != expected_tool:
        return "fail", f"expected tool '{expected_tool}' but agent called '{calls[0]}'"
    return "pass", f"expected tool called ({expected_tool})"


# SHAPE C: the one test whose assertion is a cascade, not a single rule. R20 asks
# for a joke with joke history already seeded: calling tell_joke is a PASS, and
# only the no-tool branch needs any judgment ("did she tell one herself, or
# deflect?"). Classifying it with the no-tool tests reports five false failures.
SHAPE_C_TESTS = ("R20",)


def is_shape_c(run):
    name = run.get("test_name") or ""
    return any(f" {t} " in f" {name} " or name.endswith(t) or t in name.split()
               for t in SHAPE_C_TESTS)


def expected_from_run(run):
    """What did this stored EL run expect? Read it from THEIR data, not ours.

    Tool-type runs carry the expected tool in their rationale message; llm-type
    runs in this suite are no-tool assertions, except the shape-C cascade.
    """
    ti = run.get("test_info") or {}
    if (ti.get("type") or run.get("metadata", {}).get("test_type")) == "tool":
        rat = json.dumps(run.get("condition_result") or {})
        for name in BUSINESS_TOOLS:
            if f"Expected tool '{name}'" in rat or f"expected tool '{name}'" in rat:
                return name
        # passing tool runs say only "Expected tool called": recover the
        # expectation from what actually fired, which their checker approved.
        calls = new_business_calls(run.get("agent_responses"))
        return calls[0] if calls else "UNKNOWN"
    return None


def replay(path):
    """Grade EL's stored corpus with our rules and diff against their verdicts."""
    with open(path) as fh:
        data = json.load(fh)
    runs = data.get("test_runs") or []
    agree = disagree = errors = undecided = 0
    diffs = []
    by_type = {"tool": [0, 0], "llm": [0, 0]}

    for run in runs:
        their = ((run.get("condition_result") or {}).get("result") or "").lower()
        their_pass = their == "success"
        ttype = (run.get("test_info") or {}).get("type") or \
                (run.get("metadata") or {}).get("test_type") or "?"
        expected = expected_from_run(run)
        if expected == "UNKNOWN":
            errors += 1
            continue
        if is_shape_c(run):
            calls = new_business_calls(run.get("agent_responses"))
            if calls == ["tell_joke"]:
                verdict, detail = "pass", "shape C: tool called -> pass"
            else:
                verdict, detail = "undecided", "shape C: no tool -> tier-2 judge"
        else:
            verdict, detail = grade(expected, run.get("agent_responses"))
        if verdict == "undecided":
            undecided += 1
            continue
        if verdict == "error":
            errors += 1
            continue
        ours_pass = verdict == "pass"
        if ttype in by_type:
            by_type[ttype][0] += 1
            by_type[ttype][1] += 1 if ours_pass == their_pass else 0
        if ours_pass == their_pass:
            agree += 1
        else:
            disagree += 1
            diffs.append((run.get("test_name"), their, verdict, detail,
                          ((run.get("condition_result") or {}).get("rationale") or {}).get("summary", "")))

    total = agree + disagree
    print(f"replayed {len(runs)} stored runs")
    print(f"  graded:     {total}   (skipped {errors} instrument, {undecided} -> tier-2 judge)")
    print(f"  agreement:  {agree}/{total}" + (f"  ({100*agree/total:.1f}%)" if total else ""))
    for t, (n, ok) in by_type.items():
        if n:
            print(f"    {t:5} tests: {ok}/{n} agree")
    if diffs:
        print(f"\n  {len(diffs)} DISAGREEMENT(S) -- each needs a human read:")
        for name, their, ours, detail, rationale in diffs:
            print(f"    {name}: EL said {their!r}, we say {ours!r}")
            print(f"      our reason:   {detail}")
            print(f"      their reason: {rationale[:150]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", metavar="INVOCATION_JSON",
                    help="grade a stored ElevenLabs invocation and diff vs their verdicts")
    a = ap.parse_args()
    if a.replay:
        return replay(a.replay)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# CONVERSATIONAL-QUALITY CHECKS (2026-08-02). The suite grades TOOL CHOICE, so
# R32-R39 (the acknowledgement probes: "Thanks.", "Mm-hmm.", "Sure.", "Oops.")
# all pass by calling no tool -- and nothing looks at what she actually SAID.
# Measured by hand on 8 acknowledgements, both of her own rules were breaking
# underneath a green suite:
#   NEVER DEAD-END          -> 1/8 dead-ended ("Sure." -> "Still here.")
#   NEVER SAY THE SAME
#   OFFER TWICE             -> "You got it. Anything else I can help with?"
#                              appeared 4 of 8 times, verbatim
# These are cheap and deterministic: they reuse replies the run already
# captured, cost no extra conversations, and are reported ALONGSIDE the verdict
# rather than flipping it -- a dead-end is a quality signal, not a routing bug,
# and conflating them would make the tool-choice metric mean two things.
ACK_PROBES = ("R32", "R33", "R34", "R35", "R36", "R37", "R38", "R39")


# MID-JOKE-RUN INVERTS THE RULE (2026-08-03). Her prompt: "Mid-run through
# jokes... a half-line riffing on the joke, a dry aside, agreeing it was thin -
# any of those keep it open... What you must NOT do mid-run is ask 'anything
# else I can help with?', because that ends the run of jokes he was working
# through." So inside a joke run a bare riff is CORRECT and the offer phrase is
# the DEFECT -- the exact opposite of the general acknowledgement rule.
# Six of the eight ack probes (R32-R37) carry "Tell me a joke." as context, so
# scoring them with the general rule marked her right answers wrong and her
# wrong answers right. A clause added on that bad signal on 2026-08-03 pushed
# her toward the forbidden phrase and was reverted.
_MIDRUN_FORBIDDEN = re.compile(r"anything else .{0,12}help", re.I)
_BARE_CLOSER = re.compile(
    r"^\W*(you got it|no worries|of course|sure|still here|got it|okay)\W*$", re.I)


def dead_ends(reply, mid_joke_run=False):
    """True when the beat gives the user nowhere to go.

    Her rule: "'You got it.' / 'No worries.' / 'Of course.' on their own are
    dead ends: you have replied and handed him silence." An opening is a
    question, or an explicit standing offer he can pick up later.
    """
    if not (reply or "").strip():
        return False                      # nothing captured is an instrument issue
    if mid_joke_run:
        # THE OFFER PHRASE IS NO LONGER A DEFECT (2026-08-04, measured).
        #
        # This branch used to return True on "anything else I can help with?"
        # because her prompt states that phrase "ends the run of jokes he was
        # working through". Two real 917 calls disproved the premise:
        #
        #   22:35  joke -> "Thanks." -> offer -> "Next." -> tell_joke fired
        #   22:41  joke -> "Next." -> joke -> "Thanks." -> offer -> "Next."
        #          -> tell_joke fired      (two deep, a real run)
        #
        # The run survives the phrase, so scoring it as a defect made the suite
        # red on known-good behaviour -- 5/8 on 08-04, 14/24 earlier the same
        # day. That is precisely the failure this runner's own docstring warns
        # about: "A gate that reds on known-good behaviour gets muted, and a
        # muted gate is the silent failure."
        #
        # She also produces a better line unprompted ("Another one, or are you
        # all set?", 22:41:51), so this was never her reciting one canned exit.
        #
        # The BARE-CLOSER half stays: a reply that is only "you got it" with no
        # content genuinely hands him silence, and that is the rule the user wrote
        # on 2026-07-27 ("it's awkward silence"). It has not fired yet.
        #
        # DO NOT restore the offer check without new evidence that a run
        # actually dies. The prompt sentence alone is not evidence; it is the
        # claim that was tested.
        return bool(_BARE_CLOSER.match(reply.strip()))
    if "?" in reply:
        return False
    return not re.search(r"still here if you need|let me know|whenever you|"
                         r"i'?m here|give me a shout", reply, re.I)


# Everything she outputs is SPOKEN. Her highest-priority prompt rule is "Say
# ONLY the words meant for his ears. Never speak your reasoning, plans,
# self-instructions" -- so a reply that opens with a scaffolding token means the
# model's own thinking reached the phone line.
#
# SEEN ONCE, 2026-08-04, on a real 917 call: the answer began with the bare word
# "thought" on its own line, then the real answer. Ten further calls (five mine
# replaying the identical TwiML, plus one of the user's own) did NOT reproduce it,
# so the RATE is about 1 in 10 and there is no known mechanism. That is exactly
# why this lives here instead of in a hunt: the suite already collects every
# reply, so catching it costs nothing and arrives with the probe and config
# attached, whereas chasing a 1-in-10 by hand costs a paid call per attempt.
#
# Deliberately NARROW. It anchors at the START of the reply and requires the
# token to stand alone on its line, because "I thought you meant..." and "my plan
# for Saturday" are ordinary speech she should be free to use.
_LEAK_TOKENS = (r"thoughts?|thinking|reasoning|analysis|plan|planning|"
                r"step \d+|assistant|system|tool_call|function_call")
_LEAKED_RE = re.compile(r"^\W*(?:" + _LEAK_TOKENS + r")\W*$", re.I)

# A NARROWER set for the newline-stripped form, where the only thing separating a
# leak from ordinary speech is a following capital. "Plan B it is" and "Thoughts
# on that?" are things she may legitimately say; "thought You sent me..." is not.
# Tokens that can open a real sentence are therefore only counted when they stand
# alone on their own line.
_LEAK_TOKENS_INLINE = (r"thought|thinking|reasoning|analysis|"
                       r"step \d+|assistant|system|tool_call|function_call")


def leaked_reasoning(reply):
    """True when scaffolding was spoken aloud before the actual answer.

    Returns the offending first line rather than a bare True where it can, so a
    suite failure names what leaked instead of only that something did.
    """
    text = (reply or "").strip()
    if not text:
        return False                      # nothing captured is an instrument issue
    first = text.split("\n", 1)[0].strip()
    if _LEAKED_RE.match(first) and len(text.split("\n", 1)) > 1:
        return first                      # a lone scaffolding line ABOVE the answer
    # The same leak with the newline stripped: "thought You sent me a couple...".
    # The capital is the only thing separating that from ordinary speech, so the
    # lookahead must stay CASE-SENSITIVE -- under re.I, [A-Z] also matches
    # lowercase, which made this fire on "Thoughts on that? I am curious."
    m = re.match(r"^\W*(" + _LEAK_TOKENS_INLINE + r")\s+(?-i:[A-Z])", text, re.I)
    return m.group(1) if m else False


def dead_end_kind(reply, mid_joke_run=False):
    """WHICH branch of dead_ends fired. Diagnostic only -- grading is unchanged.

    The count alone merges two OPPOSITE defects (2026-08-04): mid-run, offering
    "anything else I can help with?" ends the joke run he was working through,
    while a bare "no worries" merely hands him silence. They need opposite fixes,
    and a clause added on the merged signal on 2026-08-03 pushed her toward the
    forbidden phrase and had to be reverted. Naming the branch is what stops that
    happening twice.

    Returns 'offer' | 'bare-closer' | 'no-opening' | '' (not a dead end).
    """
    if not (reply or "").strip():
        return ""
    if mid_joke_run:
        # 'offer' retired 2026-08-04 alongside the dead_ends branch: it is no
        # longer a dead end, so labelling it as one would print
        # "0/8 DEAD-ENDED [offer 5]" and read as a defect that is not counted.
        # The phrase is not lost -- the runner stores every reply verbatim, so
        # `grep "anything else"` over the run JSON still finds it if the
        # frequency ever needs watching.
        return "bare-closer" if _BARE_CLOSER.match(reply.strip()) else ""
    return "" if not dead_ends(reply) else "no-opening"


def variety(replies):
    """Distinct replies across repeats of the SAME probe.

    Her rule: "NEVER SAY THE SAME OFFER TWICE... three identical offers in a row
    is what made him say the loop was worse than the bug." Returns
    (distinct, total); distinct == 1 across 3 repeats is a loop.

    COMPUTED BUT NOT REPORTED (the user, 2026-08-02): "dead end is not good.
    Looping is fine." Repeating a correct reply is not a defect worth his
    attention, and printing it beside the real signal made the summary compete
    with itself. Still written to the per-probe JSON -- it costs nothing, and
    the data is there if that call changes. This does NOT touch her prompt: the
    NEVER-SAY-THE-SAME-OFFER-TWICE rule stays as written. What changed is what
    the suite escalates, not what she is told to do.
    """
    real = [r.strip() for r in replies if (r or "").strip()]
    return len(set(real)), len(real)
