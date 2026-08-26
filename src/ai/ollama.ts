// Local/cloud LLM integration for Aether's AI features.
//
// Backend priority (each step falls through to the next on failure):
//   0. CLOUD  — optional OpenAI-compatible base URL (any provider, or the
//               local 9router). Configured in the UI; persisted in
//               localStorage. Fastest when set.
//   1. GPU    — llama.cpp server on the RX 580 (Vulkan), started by
//               ./start-llama-gpu.sh with Jan AI's Vulkan build + Qwen3-8B
//               GGUF, ~30 tok/s vs ~4 on the 4-core CPU.
//   2. OLLAMA — CPU fallback on :11434 (llama3.1:8b).
// If all three fail we return null and callers degrade gracefully to the
// local keyword/scale generators.

const LLAMA_CPP_URL = 'http://127.0.0.1:11435/v1/chat/completions';
const OLLAMA_URL = 'http://localhost:11434/api/chat';

export type LLMBackend = 'cloud' | 'gpu' | 'ollama';

export interface LLMCloudConfig {
  baseUrl: string; // e.g. "http://localhost:8123/v1" or "https://api.openai.com/v1"
  model: string;
  apiKey: string; // optional; sent as "Authorization: Bearer"
}

const CLOUD_KEY = 'aether-llm-cloud';
let cloudConfig: LLMCloudConfig | null = null;
let lastBackend: LLMBackend | null = null; // null = no successful call yet

// Restore persisted cloud config at module load (safe in Node where
// localStorage may not exist).
try {
  if (typeof localStorage !== 'undefined') {
    const raw = localStorage.getItem(CLOUD_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (p && p.baseUrl) {
        cloudConfig = {
          baseUrl: String(p.baseUrl).trim().replace(/\/+$/, ''),
          model: String(p.model || 'gpt-4o-mini'),
          apiKey: String(p.apiKey || ''),
        };
      }
    }
  }
} catch { /* ignore storage errors */ }

export function getCloudLLMConfig(): LLMCloudConfig | null {
  return cloudConfig;
}

export function setCloudLLMConfig(cfg: LLMCloudConfig | null): void {
  const cleaned = cfg && cfg.baseUrl.trim()
    ? {
        baseUrl: cfg.baseUrl.trim().replace(/\/+$/, ''),
        model: cfg.model.trim() || 'gpt-4o-mini',
        apiKey: cfg.apiKey.trim(),
      }
    : null;
  cloudConfig = cleaned;
  try {
    if (typeof localStorage !== 'undefined') {
      if (cleaned) localStorage.setItem(CLOUD_KEY, JSON.stringify(cleaned));
      else localStorage.removeItem(CLOUD_KEY);
    }
  } catch { /* ignore storage errors */ }
}

/** Which backend actually answered the last successful chat call. */
export function getLastBackend(): LLMBackend | null {
  return lastBackend;
}

interface ChatMsg {
  role: string;
  content: string;
}

interface ChatResult {
  content: string;
  backend: LLMBackend;
}

/**
 * Parse an OpenAI-compatible chat completion response body.
 * Tolerant of proxies (e.g. 9router) that append a `data: [DONE]`
 * SSE terminator after the JSON object — plain JSON.parse fails on
 * those, so we fall back to extracting the brace region.
 */
async function parseChatJson(res: Response): Promise<any> {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    const start = text.indexOf('{');
    const end = text.lastIndexOf('}');
    if (start >= 0 && end > start) {
      return JSON.parse(text.slice(start, end + 1));
    }
    throw new Error('no JSON object in chat completion response');
  }
}

/**
 * Try cloud (if configured) → GPU llama.cpp → Ollama. Returns the assistant
 * text + which backend answered, or null if every backend failed. Never throws.
 */
async function chatCompletion(
  messages: ChatMsg[],
  opts: { temperature?: number; top_p?: number } = {}
): Promise<ChatResult | null> {
  const temperature = opts.temperature ?? 0.75;
  const top_p = opts.top_p ?? 0.9;

  // 0) Cloud endpoint (OpenAI-compatible), when configured
  if (cloudConfig) {
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (cloudConfig.apiKey) headers['Authorization'] = `Bearer ${cloudConfig.apiKey}`;
      const res = await fetch(`${cloudConfig.baseUrl}/chat/completions`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          model: cloudConfig.model,
          messages,
          stream: false,
          temperature,
          top_p,
          max_tokens: 2048,
        }),
      });
      if (res.ok) {
        const data = await parseChatJson(res);
        const content = data?.choices?.[0]?.message?.content;
        if (typeof content === 'string' && content.trim()) {
          lastBackend = 'cloud';
          return { content: content.trim(), backend: 'cloud' };
        }
      } else {
        console.warn(`Cloud LLM (${cloudConfig.model}) returned ${res.status} — falling back to local`);
      }
    } catch (err) {
      console.warn('Cloud LLM call failed (falling back to local):', err);
    }
  }

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
      const data = await parseChatJson(res);
      const content = data?.choices?.[0]?.message?.content;
      if (typeof content === 'string' && content.trim()) {
        lastBackend = 'gpu';
        return { content: content.trim(), backend: 'gpu' };
      }
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
        model: 'gpt-oss:120b-cloud', // cloud proxy — zero local resources, ~0.3s response
        messages,
        stream: false,
        options: { temperature, top_p },
      }),
    });
    if (!res.ok) throw new Error('Ollama request failed: ' + res.status);
    const data = await res.json();
    const content = data?.message?.content || data?.response || '';
    const cleaned = content.trim();
    if (cleaned) {
      lastBackend = 'ollama';
      return { content: cleaned, backend: 'ollama' };
    }
    return null;
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

  const result = await chatCompletion(
    [
      { role: 'system', content: system },
      { role: 'user', content: prompt }
    ],
    { temperature: 0.75, top_p: 0.9 }
  );
  if (!result) return null;

  // Try to extract JSON
  const jsonMatch = result.content.match(/\{[\s\S]*\}/);
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
// Uses the same cloud → GPU → Ollama chain. Falls back gracefully.

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

  const result = await chatCompletion(
    [
      { role: 'system', content: system },
      { role: 'user', content: userMsg }
    ],
    { temperature: 0.8, top_p: 0.92 }
  );
  return result ? result.content : null;
}
