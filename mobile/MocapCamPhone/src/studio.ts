// Connexion au PC (MOCAM Studio) : WebSocket + reconnexion automatique.

export type StudioCommand =
  | { t: 'welcome'; id: string; name: string; prefer60?: boolean; preview?: number; lens?: 'wide' | 'ultra'; fov?: number }
  | { t: 'ping'; s0: number }
  | { t: 'arm'; take: string }
  | { t: 'start'; take: string; at: number }
  | { t: 'stop' }
  | { t: 'lock'; fast_shutter?: boolean }
  | { t: 'unlock' }
  | { t: 'snapshot' }
  | { t: 'identify' }
  | { t: 'rename'; name: string }
  | { t: 'preview'; fps: number }
  | { t: 'config'; prefer60?: boolean; preview?: number; lens?: 'wide' | 'ultra'; fov?: number }
  | { t: 'error'; message: string }

export interface ConnectInfo {
  hosts: string[]
  port: number
  token: string
}

/** JSON.stringify qui accepte les BigInt (renvoyés par certaines API iOS). */
export function toJson(v: unknown): string {
  return JSON.stringify(v, (_k, x) => (typeof x === 'bigint' ? Number(x) : x))
}

export type ConnState = 'idle' | 'connecting' | 'connected' | 'error'

/** QR code affiché par MOCAM Studio (seul moyen de connexion). */
export function parseQr(value: string): ConnectInfo | null {
  try {
    const d = JSON.parse(value)
    if (d && d.mocapcam && Array.isArray(d.hosts) && d.hosts.length && d.port && d.token) {
      return { hosts: d.hosts.map(String), port: Number(d.port), token: String(d.token) }
    }
  } catch {
    // pas un QR code de MOCAM Studio
  }
  return null
}

export class StudioClient {
  private ws: WebSocket | null = null
  private closedByUser = false
  private retry: ReturnType<typeof setTimeout> | null = null
  host: string | null = null

  constructor(
    private info: ConnectInfo,
    private getHello: () => Record<string, unknown>,
    private onCommand: (cmd: StudioCommand) => void,
    private onState: (s: ConnState, detail?: string) => void,
  ) {}

  get baseUrl(): string | null {
    return this.host ? `http://${this.host}:${this.info.port}` : null
  }

  get token(): string {
    return this.info.token
  }

  start() {
    this.closedByUser = false
    this.tryHosts(0)
  }

  stop() {
    this.closedByUser = true
    if (this.retry) clearTimeout(this.retry)
    this.ws?.close()
    this.ws = null
    this.onState('idle')
  }

  /** Essaie toutes les adresses du PC en même temps (Wi-Fi, partage de connexion, câble…) :
   *  la première qui répond est gardée. */
  private tryHosts(_i = 0) {
    if (this.closedByUser) return
    const hosts = this.host ? [this.host, ...this.info.hosts.filter((h) => h !== this.host)] : this.info.hosts
    this.onState('connecting', 'recherche du PC…')
    let won = false
    let pending = hosts.length
    const sockets: WebSocket[] = []
    const giveUp = () => {
      if (won || this.closedByUser) return
      this.host = null
      this.onState('error', 'PC introuvable : même Wi-Fi (ou partage de connexion) ? Nouvel essai…')
      this.retry = setTimeout(() => this.tryHosts(0), 2000)
    }
    for (const host of hosts) {
      let ws: WebSocket
      try {
        ws = new WebSocket(`ws://${host}:${this.info.port}/ws/phone`)
      } catch {
        if (--pending === 0) giveUp()
        continue
      }
      ws.binaryType = 'arraybuffer'
      sockets.push(ws)
      const timeout = setTimeout(() => {
        if (!won || this.ws !== ws) ws.close()
      }, 4000)
      ws.onopen = () => {
        clearTimeout(timeout)
        if (won || this.closedByUser) {
          ws.close()
          return
        }
        won = true
        for (const o of sockets) if (o !== ws) o.close()
        this.attach(ws, host)
      }
      ws.onclose = () => {
        clearTimeout(timeout)
        if (!won && --pending === 0) giveUp()
      }
      ws.onerror = () => {
        // onclose suit toujours
      }
    }
  }

  private attach(ws: WebSocket, host: string) {
    this.host = host
    this.ws = ws
    ws.send(toJson({ t: 'hello', token: this.info.token, ...this.getHello() }))
    this.onState('connected', host)
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return
      let cmd: StudioCommand
      try {
        cmd = JSON.parse(ev.data)
      } catch {
        return
      }
      if (cmd.t === 'ping') {
        // réponse immédiate pour la mesure d'horloge
        ws.send(JSON.stringify({ t: 'pong', s0: cmd.s0, c: Date.now() }))
        return
      }
      this.onCommand(cmd)
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      if (this.closedByUser) return
      this.onState('connecting', 'connexion perdue, reconnexion…')
      this.retry = setTimeout(() => this.tryHosts(0), 1000)
    }
    ws.onerror = () => {
      // onclose suit toujours
    }
  }

  send(msg: Record<string, unknown>) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(toJson(msg))
  }

  sendBinary(kind: number, jpeg: ArrayBuffer) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    // évite d'accumuler des images si le Wi-Fi est lent
    if (kind === 1 && (this.ws as any).bufferedAmount > 400_000) return
    const out = new Uint8Array(jpeg.byteLength + 1)
    out[0] = kind
    out.set(new Uint8Array(jpeg), 1)
    this.ws.send(out.buffer)
  }
}
