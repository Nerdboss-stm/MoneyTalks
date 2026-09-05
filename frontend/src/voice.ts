import { API } from "./ws";
import { send } from "./ws";

export interface VoiceHooks {
  onRecording: (on: boolean) => void;
  onAnswer: (out: any) => void;
  onError: (msg: string) => void;
  isReplaying: () => boolean;
}

/* Half-duplex push-to-talk. Record only while Space is held; never record while audio plays.
   Replay pauses while recording and while the answer plays. */
export class Voice {
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private stream: MediaStream | null = null;
  private audio: HTMLAudioElement | null = null;
  playing = false;
  recording = false;
  private pausedReplay = false;

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
    this.pauseReplay();
    this.hooks.onRecording(true);
  }

  async stop(): Promise<void> {
    if (!this.recording || !this.recorder) return;
    const rec = this.recorder;
    this.recording = false;
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

  /* Plays a streamed or cached url; starts on the first chunk the browser can decode. */
  async play(url: string | null | undefined, agentId: string | null | undefined): Promise<void> {
    if (!url) {
      await this.released(agentId);
      return;
    }
    this.playing = true;
    this.pauseReplay();
    const a = new Audio(`${API}${url}`);
    this.audio = a;
    const finish = async () => {
      if (this.audio !== a) return;
      this.audio = null;
      this.playing = false;
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
