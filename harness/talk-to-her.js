// Text conversation with the ElevenLabs agent — real agent, real tools, no audio.
//
// TURN ADVANCE (rewritten after a measurement audit). The old version advanced
// on a blind `setTimeout(askNext, 14000)`. That was wrong in two ways:
//
//   1. It made latency unmeasurable. Every single-turn probe reported 20.5s and
//      every two-turn probe 34.8s — 14s/turn plus setup, constant, identical
//      for every model. The harness was timing itself, not the agent.
//   2. It truncated slow tools, and truncated them UNEVENLY. Observed stream
//      for "any important emails today": filler at 1.7s, check_notes returns at
//      12.6s, real answer at 13.7s — three tenths inside the wall. Anything
//      that slows first-token (a thinking budget, a different model) pushes
//      those past 14s and they record as "no tool called". The bias points the
//      same direction as the effect an A/B is trying to measure, so a sweep on
//      the old transport would confidently report that thinking makes routing
//      worse when all it did was make it later.
//
// The rule now: a turn is over when her ANSWER has landed, not when a clock
// says so.
//   - tool seen        -> wait for the agent_response that follows it, then
//                         SETTLE_MS of quiet, then advance
//   - no tool yet      -> hold until NO_TOOL_GRACE_MS before concluding none is
//                         coming (the observed tool arrived at 12.6s)
//   - either way       -> HARD_MAX_MS is the backstop so one wedged turn cannot
//                         hang a 56-probe sweep
//
// Net effect on runtime: most probes get FASTER (a weather call settles in ~5s
// instead of always burning 14s); only the genuine no-tool probes pay the full
// grace period.
//
// GREETING CONTAMINATION: her opening line ("Hey, it is AJ. Still up?")
// arrives as an agent_response too, then gets cut by our user_message, which
// the server signals with `interruption` / `agent_response_correction`. The old
// version kept that text and reported it as her reply. Any reply followed by
// an interruption is now discarded.
const WS = require('ws');
const { execSync } = require('child_process');

const AGENT = process.env.ELEVENLABS_AGENT_ID;
const KEY = process.env.ELEVENLABS_API_KEY;
if (!AGENT || !KEY) {
  console.error('Set ELEVENLABS_AGENT_ID and ELEVENLABS_API_KEY (see .env.example).');
  process.exit(1);
}

const SETTLE_MS        = 2500;   // quiet after her answer before we call the turn done
const NO_TOOL_GRACE_MS = 17000;  // how long to believe a tool might still be coming
const HARD_MAX_MS      = 90000;  // backstop per turn (covers a deferred slow tool)

// A holding line means a SLOW tool is coming — the long consult tool runs
// 10-20s and has been observed past the 17s grace, which made one case grade
// "got []" while EL's own transcript showed the tool called every time
// (an instrument fail, not an agent fail). Same fix as the voice harness:
// when her reply is a holding line and no tool has landed yet, extend the
// grace once instead of concluding "no tool".
const DEFER_RE = /\b(one (moment|sec)|hang on|give me a (sec|moment)|checking|let me (check|look|see|have a squiz)|pulling (that|it) up|i'?ll (check|look|pull))\b/i;
const DEFER_GRACE_MS = 55000;

// questions to ask her, one per turn
const QUESTIONS = process.argv.slice(2);
if (!QUESTIONS.length) QUESTIONS.push("what time is it right now?");

(async () => {
  const signed = JSON.parse(execSync(
    `curl -s "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id=${AGENT}" -H "xi-api-key: ${KEY}"`
  ).toString()).signed_url;

  const ws = new WS(signed);
  let qi = 0;

  // per-turn state
  let turnActive   = false;
  let toolSeen     = false;  // a tool responded during this turn
  let answerSeen   = false;  // an agent_response arrived that counts as her answer
  let lastReply    = '';
  let deferExtended = false; // holding-line grace extension used this turn
  let settleTimer  = null;
  let graceTimer   = null;
  let hardTimer    = null;

  const clearTurnTimers = () => {
    clearTimeout(settleTimer); clearTimeout(graceTimer); clearTimeout(hardTimer);
    settleTimer = graceTimer = hardTimer = null;
  };
  const done = () => { clearTurnTimers(); try { ws.close(); } catch(e){} setTimeout(()=>process.exit(0), 300); };

  function finishTurn() {
    if (!turnActive) return;
    turnActive = false;
    clearTurnTimers();
    askNext();
  }

  // Called after every event that could mean her answer has landed.
  function maybeSettle() {
    if (!turnActive || !answerSeen) return;
    // No tool has responded yet and the grace window is still open: one might
    // still be coming, so a bare reply is filler, not an answer. Keep waiting.
    if (!toolSeen && graceTimer) return;
    // Otherwise the reply in hand is the real one. If a tool ran, answerSeen was
    // reset when it responded, so this is necessarily the post-tool reply.
    clearTimeout(settleTimer);
    settleTimer = setTimeout(finishTurn, SETTLE_MS);
  }

  ws.on('open', () => {
    ws.send(JSON.stringify({ type: 'conversation_initiation_client_data' }));
  });

  ws.on('message', (raw) => {
    let m; try { m = JSON.parse(raw.toString()); } catch(e){ return; }
    switch (m.type) {
      case 'conversation_initiation_metadata':
        askNext();
        break;

      case 'ping':
        ws.send(JSON.stringify({ type: 'pong', event_id: m.ping_event?.event_id }));
        break;

      case 'interruption':
      case 'agent_response_correction':
        // Whatever she was saying got cut off — almost always the greeting
        // being interrupted by our question. Drop it; it is not her answer.
        if (turnActive && !toolSeen) { answerSeen = false; lastReply = ''; clearTimeout(settleTimer); }
        break;

      case 'agent_response': {
        const txt = m.agent_response_event?.agent_response?.trim();
        if (!txt) break;
        console.log(`HER: ${txt}`);
        if (turnActive) {
          lastReply = txt; answerSeen = true;
          // Holding line + no tool yet: she promised a lookup, so believe her
          // longer than the default grace. Extend once per turn.
          if (!toolSeen && graceTimer && !deferExtended && DEFER_RE.test(txt)) {
            deferExtended = true;
            clearTimeout(graceTimer);
            graceTimer = setTimeout(() => { graceTimer = null; maybeSettle(); }, DEFER_GRACE_MS);
          }
          maybeSettle();
        }
        break;
      }

      case 'agent_tool_response': {
        // ElevenLabs renamed this field: the payload now arrives as
        // `agent_tool_response`, not `agent_tool_response_event`. Reading only
        // the old name printed "[tool: undefined]" for every call. Accept both
        // so this keeps working either way.
        const t = m.agent_tool_response || m.agent_tool_response_event || {};
        console.log(`   [tool: ${t.tool_name} -> ${t.is_error ? 'ERROR' : 'ok'}]`);
        if (turnActive) {
          toolSeen = true;
          clearTimeout(graceTimer); graceTimer = null;  // a tool did come; grace is moot
          // Anything she said before the tool was filler ("Let me have a squiz").
          // Wait for the reply that comes after.
          answerSeen = false; lastReply = '';
          clearTimeout(settleTimer);
        }
        break;
      }

      case 'client_tool_call':
        console.log(`   [client_tool_call: ${m.client_tool_call?.tool_name}]`);
        break;

      case 'user_transcript':
      default: break;
    }
  });

  ws.on('error', (e) => { console.log('WS ERROR:', e.message); done(); });
  ws.on('close', () => { process.exit(0); });

  function askNext() {
    if (qi >= QUESTIONS.length) { setTimeout(done, 500); return; }
    const q = QUESTIONS[qi++];
    console.log(`ME:  ${q}`);
    ws.send(JSON.stringify({ type: 'user_message', text: q }));

    turnActive = true; toolSeen = false; answerSeen = false; lastReply = ''; deferExtended = false;
    clearTurnTimers();

    // No tool yet? Hold off concluding "she called nothing" until the grace
    // window has passed — a tool call has been observed arriving as late as
    // 12.6s. Only then does a bare reply count as a finished turn.
    graceTimer = setTimeout(() => { graceTimer = null; maybeSettle(); }, NO_TOOL_GRACE_MS);
    hardTimer  = setTimeout(finishTurn, HARD_MAX_MS);
  }
})();
