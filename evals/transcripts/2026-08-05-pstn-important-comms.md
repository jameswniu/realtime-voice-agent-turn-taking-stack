# PSTN receipt — the important-communications route, verified on a real call

**2026-08-05, 13:48 PT · 47 seconds · 8 kHz PSTN ·
[listen to this call, turn by turn](https://jameswniu.github.io/realtime-voice-agent-turn-taking-stack/receipts.html)**

This is a real phone call, placed by the harness at the E2E layer: dialed from the
owner's own verified caller ID, speaking his cloned voice via TwiML `<Play>` (never
`<Say>` — a stock TTS voice transcribes too cleanly to test anything acoustic).
It replays, word for word including the stutter, a production miss from earlier the
same day: a generic "important communications" ask that the agent had answered by
opening Telegram — the one channel that holds her own conversation with the owner,
so she read his agent thread back to him as if it were news.

The fix under test: important communications are defined as **email (all three wired
inboxes) + iMessage + WhatsApp, exactly these** — enforced in the tool server's
consult recipe and in the `check_telegram` tool description, not in the persona
prompt (prompt clauses measured worse three ways; scope lives in tools and code).

## Mechanics

| | |
|---|---|
| transport | Twilio PSTN, 8 kHz μ-law |
| from | the owner's own verified caller ID |
| probe voice | his ElevenLabs clone, rendered pcm_16000 then downsampled to 8 kHz |
| turn driver | TwiML `<Pause>`/`<Play>` from a `Visibility=protected` asset domain |
| ended by | the probe's 40s TwiML window expiring mid-readout (instrument, not agent) |

## Transcript

Substance is redacted, shape is preserved; the `[markers]` are visible on purpose.

```
agent  Hey [owner], it is AJ. What are we sorting out?
user   Hey, what, what important, like, communication do I have right now?
agent  Let me check my notes for any important communications.
agent  [tool: check_notes]
agent  Okay, so you've got a few emails: document requests from [name] at
       [law firm], a [benefits provider] update about your [brokerage]
       transfer, and a [job board] notice. On iMessage, you have recent
       texts from [friend] and [friend]. Nothing new on WhatsApp, though.
```

## What this verifies

1. **Routing**: the generic comms ask went to `check_notes` — not `check_telegram`,
   which is what the morning's failing call did. One holding line, said once.
2. **ASR through the hard lane**: the stuttered, disfluent phrasing survived the
   8 kHz codec verbatim.
3. **The defined sweep**: her answer walks exactly the three sources — email,
   iMessage, WhatsApp — and names the quiet channel instead of silently skipping it.
4. **Latency**: the consult returned inside one deferral window; no retries, no
   fail-loud.

One artifact: the call ends mid-sentence because the probe's pause budget ran out,
not because she hung up — `termination_reason` says the remote (probe) side closed.
A longer window is a one-number change; the routing verdict does not depend on it.
