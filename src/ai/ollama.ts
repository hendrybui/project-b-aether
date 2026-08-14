// Local LLM integration. Primary backend: the llama.cpp server on the GPU
// (Vulkan, http://127.0.0.1:11435 — started by ./start-llama-gpu.sh with Jan
// AI's Vulkan build + Qwen3-8B GGUF, ~30 tok/s vs ~4 on the 4-core CPU).
// Fallback: Ollama on :11434. If both are down we return null and the callers
// degrade gracefully to the local keyword/scale generators.

const LLAMA_CPP_URL = 'http://127.0.0.1:11435/v1/chat/completions';
const OLLAMA_URL = 'http://localhost:11434/api/chat';

interface ChatMsg {
  role: string;
  content: string;
}

/**
 * Try the GPU llama.cpp server first, then Ollama. Returns the assistant text
 * or null if neither backend answered. Never throws.
 */
async function chatCompletion(
  messages: ChatMsg[],
  opts: { temperature?: number; top_p?: number } = {}
): Promise<string | null> {
  const temperature = opts.temperature ?? 0.75;
  const top_p = opts.top_p ?? 0.9;

  // 1) GPU llama.cpp server (OpenAI-compatible endpoint)
  try {
    const res = await fetch(LLAMA_CPP_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages,
        stream: false,
        temperature,
        top_p,
        max_tokens: 2048,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      const content = data?.choices?.[0]?.message?.content;
      if (typeof content === 'string' && content.trim()) return content.trim();
    }
  } catch (err) {
    console.warn('llama.cpp (GPU) call failed:', err);
  }

  // 2) Ollama (CPU) fallback
  try {
    const res = await fetch(OLLAMA_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'llama3.1:8b', // good balance; user can have others
        messages,
        stream: false,
        options: { temperature, top_p },
      }),
    });
    if (!res.ok) throw new Error('Ollama request failed: ' + res.status);
    const data = await res.json();
    const content = data?.message?.content || data?.response || '';
    const cleaned = content.trim();
    return cleaned || null;
  } catch (err) {
    console.warn('Ollama call failed:', err);
    return null;
  }
}

export async function describeToPatchWithOllama(prompt: string): Promise<any> {
  const system = `You are an expert sound designer for a subtractive synthesizer.
Given a short natural language description, output ONLY a compact JSON object with these keys (use values between 0 and 1 unless noted):

osc1Wave (0=sine,1=saw,2=square,3=triangle)
osc1Detune (-50 to 50)
osc1Level (0-1)
osc2Wave (same)
osc2Detune
osc2Level
subLevel (0-1)
noiseLevel (0-1)
noiseType (0 or 1)
filterCutoff (0-1)
filterRes (0-1)
filterEnvAmt (0-1)
ampAttack, ampDecay, ampSustain, ampRelease (0-1)
filterEnvAttack, filterEnvDecay (0-1)
lfo1Rate (0-1), lfo1Amount (0-1)
delayMix, reverbMix (0-1)

Respond with ONLY the JSON. No explanation.`;

  const content = await chatCompletion(
    [
      { role: 'system', content: system },
      { role: 'user', content: prompt }
    ],
    { temperature: 0.75, top_p: 0.9 }
  );
  if (!content) return null;

  // Try to extract JSON
  const jsonMatch = content.match(/\{[\s\S]*\}/);
  if (jsonMatch) {
    try {
      return JSON.parse(jsonMatch[0]);
    } catch {
      return null;
    }
  }
  return null;
}

// ===== AudioMass Bridge: Deeper LLM use for the Aether + AudioMass pair =====
// Generates copy-paste friendly text: variation ideas, stem suggestions, processing tips
// or rich descriptions of the synth sound optimized for multitrack editing workflows.
// Always uses local Ollama only. Falls back gracefully.

export async function generateAudioMassIdeaWithOllama(
  patchDesc: string,
  amContext: string = '',
  mode: 'variation' | 'describe' = 'variation'
): Promise<string | null> {
  const baseSystem = `You are an expert music producer bridging Aether (local AI subtractive synth) and AudioMass (professional multitrack waveform editor with stem support).

You help users move from synth patch to polished multitrack project. Output is ALWAYS clean, concise, copy-paste friendly prose or bullets. No JSON, no code fences unless part of an example. Be practical and inspiring for 4-12 track sessions.`;

  let system = baseSystem;
  let userMsg = `Current Aether patch description:\n${patchDesc}\n\n`;

  if (amContext) {
    userMsg += `Context from user's recent AudioMass project(s):\n${amContext}\n\n`;
  }

  if (mode === 'variation') {
    system += `

TASK: Generate a "variation for AudioMass editor".
- Open with a 1-2 sentence project concept / prompt the user can save as a note or reuse in other tools.
- Then give 3-6 specific, actionable ideas for the exported .wav from this patch inside AudioMass:
  * Suggested additional layers/stems (e.g. "bounce a lowpassed duplicate as 'sub layer'", "highpassed + long reverb as 'halo texture'")
  * Processing tips: EQ, compression, sidechain if relevant to known stems, automation, grouping
  * Creative variations: time/pitch, reverse, granular-ish ideas that editor can do
  * How it might complement or fill space in a project that already has drums/bass/vocals etc.
- End with one short "ready prompt" the user could feed to other generators or notes.
- Prioritize ideas that work great after bouncing a mono/stereo synth take into the editor.`;
    userMsg += 'Produce the variation prompt + editor suggestions now.';
  } else {
    system += `

TASK: "Describe this generated audio for use in AudioMass project".
- Give a vivid but concise sonic description (timbre, frequency profile, envelope character, vibe).
- Suggest 2-4 ideal roles/uses inside a multitrack (e.g. "great warm bass foundation under drums", "mid texture to sit behind vocals", "percussive stab layer for builds")
- Include 2-3 quick editor actions: "import, trim tail, high-shelf + 30% reverb send", "duplicate and pitch -7st for sub", etc.
- Make it useful as a clip comment or project note in AudioMass.`;
    userMsg += 'Produce the rich description + usage tips for the editor now.';
  }

  return chatCompletion(
    [
      { role: 'system', content: system },
      { role: 'user', content: userMsg }
    ],
    { temperature: 0.8, top_p: 0.92 }
  );
}
