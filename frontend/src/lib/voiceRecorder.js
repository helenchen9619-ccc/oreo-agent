// 语音录入（M13b）：getUserMedia + AudioContext 采集 → 降采样 16kHz 单声道 → Int16 PCM → WAV Blob。
// 不用 MediaRecorder：Chrome 产出的是 webm/opus 容器，火山录音识别不认；WAV 是纯 JS 编码，零依赖。
// 用法：const rec = createVoiceRecorder({ onTick, onAutoStop }); await rec.start(); const blob = await rec.stop();
// 注意：需 secure context（HTTPS 或 localhost），否则 getUserMedia 被浏览器拒绝。

const TARGET_RATE = 16000
const MAX_SECONDS = 60

// AudioWorklet 模块源码（Blob URL 内联，保持单文件）：把输入 Float32 帧原样抛回主线程
const WORKLET_SRC = `
class PCMCollector extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch && ch.length) this.port.postMessage(ch.slice(0))
    return true
  }
}
registerProcessor('pcm-collector', PCMCollector)
`

export function createVoiceRecorder({ onTick, onAutoStop } = {}) {
  let audioCtx = null
  let stream = null
  let source = null
  let node = null // AudioWorkletNode 或 ScriptProcessorNode
  let chunks = []
  let startTs = 0
  let timer = null
  let stopped = false

  async function start() {
    chunks = []
    stopped = false
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    })
    audioCtx = new AudioContext()
    source = audioCtx.createMediaStreamSource(stream)
    if (audioCtx.audioWorklet) {
      const modUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }))
      try {
        await audioCtx.audioWorklet.addModule(modUrl)
        node = new AudioWorkletNode(audioCtx, 'pcm-collector')
        node.port.onmessage = (e) => chunks.push(e.data)
      } finally {
        URL.revokeObjectURL(modUrl)
      }
    } else {
      // 老浏览器兜底：ScriptProcessorNode（已废弃但可用）
      const sp = audioCtx.createScriptProcessor(4096, 1, 1)
      sp.onaudioprocess = (e) => chunks.push(e.inputBuffer.getChannelData(0).slice(0))
      node = sp
    }
    source.connect(node)
    node.connect(audioCtx.destination) // worklet 无输出，仅为驱动 process 回调
    startTs = Date.now()
    timer = window.setInterval(() => {
      const secs = (Date.now() - startTs) / 1000
      onTick?.(secs)
      if (secs >= MAX_SECONDS) {
        onAutoStop?.()
      }
    }, 250)
  }

  function teardown() {
    if (timer) window.clearInterval(timer)
    timer = null
    try { source?.disconnect() } catch { /* 忽略 */ }
    try { node?.disconnect() } catch { /* 忽略 */ }
    stream?.getTracks().forEach((t) => t.stop())
    const ctx = audioCtx
    source = node = stream = null
    audioCtx = null
    return ctx?.close().catch(() => {})
  }

  // 完成录音：返回 WAV Blob（16kHz mono s16le）
  async function stop() {
    if (stopped) return null
    stopped = true
    const rate = audioCtx?.sampleRate || 48000
    const got = chunks
    chunks = []
    await teardown()
    if (!got.length) return null
    const pcm = downsampleTo16k(got, rate)
    return encodeWav(pcm)
  }

  // 放弃录音：不产生任何结果
  async function cancel() {
    stopped = true
    chunks = []
    await teardown()
  }

  return { start, stop, cancel, maxSeconds: MAX_SECONDS }
}

// Float32 chunks → 拼接 → 线性插值降采样到 16k → Int16
function downsampleTo16k(chunks, fromRate) {
  const total = chunks.reduce((n, c) => n + c.length, 0)
  const merged = new Float32Array(total)
  let off = 0
  for (const c of chunks) {
    merged.set(c, off)
    off += c.length
  }
  if (fromRate === TARGET_RATE) return floatTo16(merged)
  const ratio = fromRate / TARGET_RATE
  const outLen = Math.floor(merged.length / ratio)
  const out = new Float32Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio
    const i0 = Math.floor(pos)
    const i1 = Math.min(i0 + 1, merged.length - 1)
    out[i] = merged[i0] + (merged[i1] - merged[i0]) * (pos - i0)
  }
  return floatTo16(out)
}

function floatTo16(buf) {
  const out = new Int16Array(buf.length)
  for (let i = 0; i < buf.length; i++) {
    const v = Math.max(-1, Math.min(1, buf[i]))
    out[i] = v < 0 ? v * 0x8000 : v * 0x7fff
  }
  return out
}

// Int16 PCM → WAV（RIFF 44 字节头）
function encodeWav(pcm) {
  const buf = new ArrayBuffer(44 + pcm.length * 2)
  const v = new DataView(buf)
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)) }
  writeStr(0, 'RIFF')
  v.setUint32(4, 36 + pcm.length * 2, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  v.setUint32(16, 16, true)          // fmt 块大小
  v.setUint16(20, 1, true)           // PCM
  v.setUint16(22, 1, true)           // 单声道
  v.setUint32(24, TARGET_RATE, true) // 采样率
  v.setUint32(28, TARGET_RATE * 2, true) // 字节率
  v.setUint16(32, 2, true)           // 块对齐
  v.setUint16(34, 16, true)          // 位深
  writeStr(36, 'data')
  v.setUint32(40, pcm.length * 2, true)
  new Int16Array(buf, 44).set(pcm)
  return new Blob([buf], { type: 'audio/wav' })
}
