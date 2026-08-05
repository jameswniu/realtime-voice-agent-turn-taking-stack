// VOICE conversation with the ElevenLabs agent — real agent, real ASR, real
// turn-taking. The voice-mode sibling of talk-to-her.js (text WebSocket).
//
// WHY THIS EXISTS. The text harness sends `user_message`, which skips ASR and
// VAD entirely — it can never catch a mishearing ("flee wood" for "Fleetwood")
// or a turn-taking fault, which is exactly the class of bug that keeps
// surfacing on live calls. This one speaks: macOS `say` renders each phrase,
// afconvert resamples it to the socket's PCM format, and the bytes stream at
// REAL-TIME pace as `user_audio_chunk` frames, with continuous silence frames
// between utterances so server-side VAD sees a live microphone, not a file
// upload. Protocol per the official docs:
// https://elevenlabs.io/docs/agents-platform/api-reference/agents-platform/websocket
// ({"user_audio_chunk": "<base64 pcm>"}, format from conversation metadata).
//
// COST: this opens a real Agents-platform session, billed by ElevenLabs like
// any call minute. It is a QA tool, not a monitor — never cron it.
//
// WHAT IT PROVES that the text harness cannot:
//   - user_transcript = what ASR actually heard (printed next to what we said)
//   - answer latency measured from END OF SPEECH, like a human experiences it
//   - VAD/turn-taking: does she wait for us to finish, does she talk over us
//
// Usage: node talk-to-her-voice.js "phrase one" "phrase two" ...
//        SAY_VOICE=Samantha overrides the macOS voice (default Ava).
const WS = require('ws');
const { execSync } = require('child_process');
const fs = require('fs');

const AGENT = process.env.ELEVENLABS_AGENT_ID;
const KEY = process.env.ELEVENLABS_API_KEY;
if (!AGENT || !KEY) {
  console.error('Set ELEVENLABS_AGENT_ID and ELEVENLABS_API_KEY (see .env.example).');
  process.exit(1);
}
const VOICE = process.env.SAY_VOICE || 'Ava';
// PROBE_VOICE_ID (optional): an ElevenLabs voice ID — ideally a clone of YOUR
// OWN voice — used to render the probe phrases instead of macOS `say`. Probing
// with your real timbre is the point: `say` cannot catch an ASR mishearing of
// YOU. Costs TTS characters on top of the call minutes. Set PROBE_TTS=say to
// force the free macOS voice even when PROBE_VOICE_ID is set.
const CLONE_ID = process.env.PROBE_VOICE_ID || '';
const USE_CLONE = CLONE_ID && (process.env.PROBE_TTS || 'clone').toLowerCase() !== 'say';

const CHUNK_MS         = 250;    // one frame ≈ one real mic buffer
const SETTLE_MS        = 2500;   // quiet after her answer before the turn is done
const NO_TOOL_GRACE_MS = 4000;   // VOICE IS NOT TEXT: the text harness could
// idle 17s waiting for a late tool, but on a live call that same silence reads
// as the caller wandering off — the agent prompted 'Still there?' at 7s and
// hung up before turn 2 ever fired. So: advance fast after her answer...
const DEFER_GRACE_MS   = 55000;  // ...UNLESS the answer is a deferral ('give me a
// moment', 'building') — then hold, because a tool/completion IS coming and
// talking over it is the thing callers complain about on real calls.
//
// TWO DEFECTS FIXED HERE, both found by comparing this harness's output
// against ElevenLabs' OWN recorded transcripts (it reported a tool missing
// twice while EL recorded it firing both times):
//
//   1. DEFER_RE missed 4 of the 5 holding lines she actually uses — including
//      her most characteristic one. The deferral branch never fired, so the
//      grace never extended at all.
//   2. Even when it did fire, 20000ms was short: the long consult tool
//      measured 22-23s end to end on the voice path, so the harness gave up
//      2-3s before the tool landed.
//
// Both are the same bug fixed in talk-to-her.js hours earlier; the twin was
// left broken. Pattern below is the text harness's, kept in sync deliberately
// — if her phrasing changes, BOTH files need the new wording.
const DEFER_RE = /\b(one (moment|sec)|hold on|hang on|give me a (sec|moment)|checking|let me (check|look|see|have a squiz)|pulling (that|it) up|i'?ll (check|look|pull)|building|working on|right away|coming up)\b/i;
const HARD_MAX_MS      = 90000;  // voice turns run longer than text ones

const PHRASES = process.argv.slice(2);
if (!PHRASES.length) PHRASES.push('what time is it right now?');

// say -> WAV at the socket's sample rate -> raw PCM (data chunk only).
function renderPhrase(text, rate) {
  if (USE_CLONE) return renderPhraseClone(text, rate);
  const aiff = `/tmp/aj-voice-probe.aiff`, wav = `/tmp/aj-voice-probe.wav`;
  execSync(`say -v ${VOICE} -o ${aiff} ${JSON.stringify(text)}`);
  execSync(`afconvert -f WAVE -d LEI16@${rate} -c 1 ${aiff} ${wav}`);
  const buf = fs.readFileSync(wav);
  const i = buf.indexOf(Buffer.from('data'));       // WAV data chunk
  if (i < 0) throw new Error('no data chunk in WAV');
  return buf.subarray(i + 8);
}

// Your clone via ElevenLabs TTS. output_format=pcm_16000 hands back raw
// headerless PCM at exactly the socket's mic format — no conversion step.
function renderPhraseClone(text, rate) {
  const out = `/tmp/aj-voice-probe-clone.pcm`;
  const body = JSON.stringify({ text, model_id: 'eleven_flash_v2_5' });
  execSync(`curl -sf -X POST "https://api.elevenlabs.io/v1/text-to-speech/${CLONE_ID}?output_format=pcm_${rate}" ` +
    `-H "xi-api-key: ${KEY}" -H "Content-Type: application/json" ` +
    `-d ${JSON.stringify(body)} -o ${out}`);
  const pcm = fs.readFileSync(out);
  if (pcm.length < 2000) throw new Error(`clone TTS returned only ${pcm.length} bytes`);
  return pcm;
}

(async () => {
  const signed = JSON.parse(execSync(
    `curl -s "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id=${AGENT}" -H "xi-api-key: ${KEY}"`
  ).toString()).signed_url;

  const ws = new WS(signed);
  const t0 = Date.now();
  const ts = () => `[${((Date.now() - t0) / 1000).toFixed(1)}s]`;

  let rate = 16000;              // corrected from conversation metadata
  let bytesPerChunk = 8000;      // rate * 2 bytes * 0.25s
  let pi = 0;                    // phrase index
  let speaking = false;          // currently streaming an utterance
  let speechEndAt = 0;           // when our last utterance finished (latency zero)
  let agentAudioBytes = 0;

  // per-turn state — same discipline as talk-to-her.js
  let turnActive = false, toolSeen = false, answerSeen = false;
  let settleTimer = null, graceTimer = null, hardTimer = null;
  const clearTurnTimers = () => {
    clearTimeout(settleTimer); clearTimeout(graceTimer); clearTimeout(hardTimer);
    settleTimer = graceTimer = hardTimer = null;
  };
  const done = () => {
    clearTurnTimers(); clearInterval(silenceTimer);
    console.log(`${ts()} [agent audio received: ${(agentAudioBytes/1024).toFixed(0)} KB]`);
    try { ws.close(); } catch (e) {}
    setTimeout(() => process.exit(0), 300);
  };
  const finishTurn = () => {
    if (!turnActive) return;
    turnActive = false; clearTurnTimers(); speakNext();
  };
  const maybeSettle = () => {
    if (!turnActive || !answerSeen) return;
    if (!toolSeen && graceTimer) return;      // a tool may still be coming
    clearTimeout(settleTimer);
    settleTimer = setTimeout(finishTurn, SETTLE_MS);
  };

  // A live microphone never stops sending. Between utterances we stream
  // silence so VAD can close our turn; without this the server waits forever
  // for the "end" of an utterance that already ended.
  const sendChunk = (buf) => ws.send(JSON.stringify({ user_audio_chunk: buf.toString('base64') }));
  let silenceTimer = null;
  const startSilence = () => {
    silenceTimer = setInterval(() => {
      if (!speaking && ws.readyState === 1) sendChunk(Buffer.alloc(bytesPerChunk));
    }, CHUNK_MS);
  };

  async function speakNext() {
    if (pi >= PHRASES.length) { setTimeout(done, 4000); return; }
    const text = PHRASES[pi++];
    console.log(`${ts()} ME (speaking): ${text}`);
    const pcm = renderPhrase(text, rate);
    speaking = true;
    for (let o = 0; o < pcm.length; o += bytesPerChunk) {   // real-time pacing
      if (ws.readyState !== 1) return;
      sendChunk(pcm.subarray(o, o + bytesPerChunk));
      await new Promise(r => setTimeout(r, CHUNK_MS));
    }
    speaking = false;
    speechEndAt = Date.now();
    console.log(`${ts()} ME (done speaking, ${(pcm.length / (rate * 2)).toFixed(1)}s of audio)`);

    turnActive = true; toolSeen = false; answerSeen = false;
    clearTurnTimers();
    graceTimer = setTimeout(() => { graceTimer = null; maybeSettle(); }, NO_TOOL_GRACE_MS);
    hardTimer = setTimeout(finishTurn, HARD_MAX_MS);
  }

  ws.on('open', () => ws.send(JSON.stringify({ type: 'conversation_initiation_client_data' })));

  ws.on('message', (raw) => {
    let m; try { m = JSON.parse(raw.toString()); } catch (e) { return; }
    switch (m.type) {
      case 'conversation_initiation_metadata': {
        const meta = m.conversation_initiation_metadata_event || {};
        const fmt = meta.user_input_audio_format || 'pcm_16000';
        rate = parseInt(fmt.replace(/\D/g, ''), 10) || 16000;
        bytesPerChunk = Math.round(rate * 2 * (CHUNK_MS / 1000));
        console.log(`${ts()} [connected: mic format ${fmt}, her audio ${meta.agent_output_audio_format}]`);
        startSilence();
        // Let her greeting land before we start talking — a real caller listens
        // first. VAD-wise our silence frames are already flowing.
        setTimeout(speakNext, 5000);
        break;
      }
      case 'ping':
        ws.send(JSON.stringify({ type: 'pong', event_id: m.ping_event?.event_id }));
        break;
      case 'user_transcript': {
        const heard = m.user_transcription_event?.user_transcript?.trim();
        if (heard) console.log(`${ts()} ASR HEARD: ${heard}`);
        break;
      }
      case 'audio':
        agentAudioBytes += Math.round((m.audio_event?.audio_base_64?.length || 0) * 0.75);
        break;
      case 'interruption':
      case 'agent_response_correction':
        if (turnActive && !toolSeen) { answerSeen = false; clearTimeout(settleTimer); }
        break;
      case 'agent_response': {
        const txt = m.agent_response_event?.agent_response?.trim();
        if (!txt) break;
        const lat = speechEndAt ? ` (+${((Date.now() - speechEndAt) / 1000).toFixed(1)}s after I stopped)` : '';
        console.log(`${ts()} HER: ${txt}${lat}`);
        if (turnActive) {
          answerSeen = true;
          if (!toolSeen && graceTimer && DEFER_RE.test(txt)) {
            // She promised work; hold the turn open for the tool/completion.
            clearTimeout(graceTimer);
            graceTimer = setTimeout(() => { graceTimer = null; maybeSettle(); }, DEFER_GRACE_MS);
            console.log(`${ts()}    [deferral heard — holding turn up to ${DEFER_GRACE_MS / 1000}s]`);
          }
          maybeSettle();
        }
        break;
      }
      case 'agent_tool_response': {
        const t = m.agent_tool_response || m.agent_tool_response_event || {};
        console.log(`${ts()}    [tool: ${t.tool_name} -> ${t.is_error ? 'ERROR' : 'ok'}]`);
        if (turnActive) {
          toolSeen = true;
          clearTimeout(graceTimer); graceTimer = null;
          answerSeen = false; clearTimeout(settleTimer);
        }
        break;
      }
      case 'vad_score':
      default: break;
    }
  });

  ws.on('error', (e) => { console.log('WS ERROR:', e.message); done(); });
  ws.on('close', () => process.exit(0));
})();
