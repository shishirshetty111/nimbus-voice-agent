// AudioWorklet processors (run on the audio thread).
//   mic-capture : forwards raw mic samples to the main thread
//   pcm-player  : jitter buffer for streamed TTS audio. Playback starts once `target` ms are buffered
//                 (or the stream ended). Running dry mid-stream counts as an underrun (an audible gap).
//                 "flush" (barge-in) drops everything queued and reports how much of the turn was heard.

class MicCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("mic-capture", MicCapture);

class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];      // Float32Array chunks at the context rate
    this.offset = 0;      // read position inside queue[0]
    this.buffered = 0;    // samples queued
    this.target = 0;      // samples to buffer before starting
    this.playing = false;
    this.ended = false;
    this.starved = false;
    this.underruns = 0;
    this.played = 0;      // samples of this turn actually output
    this.port.onmessage = ({ data }) => {
      if (data.type === "chunk") {
        this.queue.push(data.samples);
        this.buffered += data.samples.length;
      } else if (data.type === "target") {
        this.target = Math.round((data.ms / 1000) * sampleRate);
      } else if (data.type === "end") {
        this.ended = true;
      } else if (data.type === "reset") {
        this.queue = [];
        this.offset = this.buffered = 0;
        this.playing = this.ended = this.starved = false;
        this.underruns = this.played = 0;
      } else if (data.type === "flush") {
        this.queue = [];
        this.offset = this.buffered = 0;
        this.playing = this.ended = this.starved = false;
        this.port.postMessage({ type: "flushed", played_ms: (this.played / sampleRate) * 1000 });
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing) {
      if (this.buffered > 0 && (this.buffered >= this.target || this.ended)) {
        this.playing = true;
        this.port.postMessage({ type: "started", buffered_ms: (this.buffered / sampleRate) * 1000 });
      } else {
        out.fill(0);
        return true;
      }
    }
    let i = 0;
    while (i < out.length && this.queue.length) {
      const head = this.queue[0];
      const n = Math.min(out.length - i, head.length - this.offset);
      out.set(head.subarray(this.offset, this.offset + n), i);
      i += n;
      this.played += n;
      this.offset += n;
      this.buffered -= n;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    if (i < out.length) {
      out.fill(0, i);
      if (this.ended) {
        this.playing = false;
        this.ended = false;
        this.port.postMessage({ type: "done", underruns: this.underruns });
      } else if (!this.starved) {
        this.starved = true;
        this.underruns++;
        this.port.postMessage({ type: "underrun", count: this.underruns });
      }
    } else {
      this.starved = false;
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}
registerProcessor("pcm-player", PcmPlayer);
