import { API } from "./ws";
import { send } from "./ws";
import { SHELL } from "./tokens";

export interface VoiceHooks {
  onRecording: (on: boolean) => void;
  onAnswer: (out: any) => void;
  onError: (msg: string) => void;
  isReplaying: () => boolean;
  onPlaying?: (on: boolean, agentId: string | null) => void;
  onLevel?: (bars: number[]) => void;
}

/* Half-duplex push-to-talk. Record only while Space is held; never record while audio plays.
   Replay pauses while recording and while the answer plays. */
export class Voice {
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private stream: MediaStream | null = null;
  private audio: HTMLAudioElement | null = null;
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private raf = 0;
  playing = false;
  recording = false;
  private pausedReplay = false;
  private speakingAgent: string | null = null;

  constructor(private hooks: VoiceHooks) {}

  async start(): Promise<void> {
    if (this.recording || this.playing) return;
    try {
      this.stream = this.stream ?? (await navigator.mediaDevices.getUserMedia({ audio: true }));
    } catch {
      this.hooks.onError("mic unavailable");
      return;
    }
    this.chunks = [];
    this.recorder = new MediaRecorder(this.stream, { mimeType: MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm" });
    this.recorder.ondataavailable = (e) => {
      if (e.data.size) this.chunks.push(e.data);
    };
    this.recorder.start(100);
    this.recording = true;
    this.meter(true);
    this.pauseReplay();
    this.hooks.onRecording(true);
  }

  async stop(): Promise<void> {
    if (!this.recording || !this.recorder) return;
    const rec = this.recorder;
    this.recording = false;
    this.meter(false);
    this.hooks.onRecording(false);
    await new Promise<void>((done) => {
      rec.onstop = () => done();
      rec.stop();
    });
    const blob = new Blob(this.chunks, { type: "audio/webm" });
    this.chunks = [];
    if (blob.size < 800) {
      this.resumeReplay();
      return;
    }
    const form = new FormData();
    form.append("audio", blob, "utterance.webm");
    try {
      const r = await fetch(`${API}/voice/utterance`, { method: "POST", body: form });
      const out = await r.json();
      this.hooks.onAnswer(out);
      await this.play(out.audio_url, out.agent_id);
    } catch (err) {
      this.hooks.onError(`voice: ${(err as Error).message}`);
      this.resumeReplay();
    }
  }

  /* Real input level from the recording stream: SHELL.meterBars bands, 0..1, once per frame while recording. */
  private meter(on: boolean): void {
    if (!on) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
      this.hooks.onLevel?.(new Array(SHELL.meterBars).fill(0));
      return;
    }
    try {
      if (!this.ctx) this.ctx = new AudioContext();
      if (!this.analyser && this.stream) {
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 64;
        this.analyser.smoothingTimeConstant = 0.6;
        this.ctx.createMediaStreamSource(this.stream).connect(this.analyser);
      }
      void this.ctx.resume();
    } catch {
      return;
    }
    const analyser = this.analyser;
    if (!analyser) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      if (!this.recording) return;
      analyser.getByteFrequencyData(data);
      const bars: number[] = [];
      for (let i = 0; i < SHELL.meterBars; i++) {
        const a = data[i * 2 + 1] ?? 0;
        const b = data[i * 2 + 2] ?? 0;
        bars.push(Math.min(1, Math.max(a, b) / 200));
      }
      this.hooks.onLevel?.(bars);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  /* Plays a streamed or cached url; starts on the first chunk the browser can decode. */
  async play(url: string | null | undefined, agentId: string | null | undefined): Promise<void> {
    if (!url) {
      await this.released(agentId);
      return;
    }
    this.playing = true;
    this.speakingAgent = agentId ?? null;
    this.hooks.onPlaying?.(true, agentId ?? null);
    this.pauseReplay();
    const a = new Audio(`${API}${url}`);
    this.audio = a;
    const finish = async () => {
      if (this.audio !== a) return; // cancel() cleared it: that path already released the agent
      this.audio = null;
      this.speakingAgent = null;
      this.playing = false;
      this.hooks.onPlaying?.(false, agentId ?? null);
      await this.released(agentId);
    };
    a.onended = () => void finish();
    a.onerror = () => void finish();
    try {
      await a.play();
    } catch {
      await finish();
    }
  }

  /* Escape: cut the answer off now. Playback stops and the replay resumes without waiting for the
     network, so this works with the backend unreachable; the release POST is best effort.
     Returns the agent that was speaking, for the caller to release on screen. */
  cancel(): string | null {
    const agentId = this.speakingAgent;
    const a = this.audio;
    const was = this.playing;
    this.audio = null;
    this.speakingAgent = null;
    this.playing = false;
    if (a) {
      try {
        a.pause();
        a.src = "";
      } catch {
        /* already torn down */
      }
    }
    if (!was) return null;
    this.hooks.onPlaying?.(false, agentId);
    this.resumeReplay();
    void this.released(agentId);
    return agentId;
  }

  async speakText(text: string, session: string | null, asOf: string | null): Promise<any> {
    const r = await fetch(`${API}/voice/text`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, session, as_of: asOf }) });
    const out = await r.json();
    this.hooks.onAnswer(out);
    await this.play(out.audio_url, out.agent_id);
    return out;
  }

  private async released(agentId: string | null | undefined): Promise<void> {
    try {
      await fetch(`${API}/voice/released`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_id: agentId ?? null }) });
    } catch {
      /* offline: the floor restores on its own */
    }
    this.resumeReplay();
  }

  private pauseReplay(): void {
    if (this.pausedReplay || !this.hooks.isReplaying()) return;
    this.pausedReplay = true;
    send("pause");
  }

  private resumeReplay(): void {
    if (!this.pausedReplay || this.recording || this.playing) return;
    this.pausedReplay = false;
    send("resume");
  }
}
