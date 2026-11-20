import { StatusBar } from 'expo-status-bar'
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ActivityIndicator,
  Image as RNImage,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  type LayoutChangeEvent,
} from 'react-native'
import { useKeepAwake } from 'expo-keep-awake'
import * as Battery from 'expo-battery'
import { File, Paths, UploadType } from 'expo-file-system'
import {
  Camera,
  CommonDynamicRanges,
  CommonResolutions,
  isScannedCode,
  useCameraDevice,
  useCameraDevices,
  useCameraPermission,
  useMicrophonePermission,
  useObjectOutput,
  usePhotoOutput,
  useVideoOutput,
  type CameraRef,
  type CameraSessionConfig,
  type Constraint,
  type Recorder,
  type ScannedObject,
  type ScannedObjectType,
} from 'react-native-vision-camera'
import { parseQr, StudioClient, type ConnectInfo, type ConnState, type StudioCommand } from './src/studio'
import { decodeQr } from './src/qr'
import type { Image } from 'react-native-nitro-image'
import {
  addPending,
  cleanName,
  getPending,
  loadSettings,
  removePending,
  saveSettings,
  type Settings,
} from './src/settings'

const APP_VERSION = '1.4.0'

// Erreurs hors affichage (minuteries, callbacks natifs) : on les montre à l'écran
// au lieu de laisser l'app se fermer.
const errorListeners = new Set<(e: Error) => void>()
const g = globalThis as any
if (g.ErrorUtils?.setGlobalHandler) {
  g.ErrorUtils.setGlobalHandler((e: Error) => {
    if (errorListeners.size === 0) console.error(e)
    errorListeners.forEach((l) => l(e))
  })
}
const QR_TYPES: ScannedObjectType[] = ['qr']

// ============================================================================ App

export default function App() {
  return (
    <ErrorBoundary>
      <AppInner />
    </ErrorBoundary>
  )
}

/** Affiche l'erreur à l'écran au lieu de fermer l'app (plus simple à diagnostiquer). */
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <View style={styles.center}>
          <StatusBar hidden />
          <Text style={styles.title}>MOCAM a rencontré une erreur</Text>
          <Text style={[styles.text, { fontSize: 13 }]} selectable>
            {String(this.state.error?.message ?? this.state.error)}
          </Text>
          <Text style={styles.small}>Faites une capture d'écran de ce message pour le diagnostic.</Text>
          <Btn label="Réessayer" onPress={() => this.setState({ error: null })} />
        </View>
      )
    }
    return this.props.children
  }
}

function AppInner() {
  const [settings, setSettings] = useState<Settings>(() => loadSettings())
  const [info, setInfo] = useState<ConnectInfo | null>(null)
  const cam = useCameraPermission()
  const mic = useMicrophonePermission()

  const updateSettings = useCallback((s: Settings) => {
    setSettings(s)
    saveSettings(s)
  }, [])

  if (!cam.hasPermission || !mic.hasPermission) {
    return (
      <View style={styles.center}>
        <StatusBar hidden />
        <RNImage source={require('./assets/logo-mocam.png')} style={styles.logo} resizeMode="contain" accessibilityLabel="MOCAM" />
        <Text style={styles.text}>L'app a besoin de la caméra et du micro (le son sert à synchroniser les téléphones).</Text>
        <Btn
          label="Autoriser"
          onPress={async () => {
            if (!cam.hasPermission) await cam.requestPermission()
            if (!mic.hasPermission) await mic.requestPermission()
          }}
        />
      </View>
    )
  }

  if (info == null) {
    return (
      <ConnectScreen
        settings={settings}
        onSettings={updateSettings}
        onConnect={(i, raw) => {
          updateSettings({ ...settings, lastQr: raw })
          setInfo(i)
        }}
      />
    )
  }
  return (
    <CaptureScreen
      info={info}
      settings={settings}
      onSettings={updateSettings}
      onDisconnect={() => setInfo(null)}
    />
  )
}

// ============================================================================ Connexion

function ConnectScreen({
  settings,
  onSettings,
  onConnect,
}: {
  settings: Settings
  onSettings: (s: Settings) => void
  onConnect: (i: ConnectInfo, raw: string) => void
}) {
  const [scanning, setScanning] = useState(true)
  const [bad, setBad] = useState(false)
  const done = useRef(false)

  const onCode = useCallback(
    (value: string) => {
      if (done.current) return
      const i = parseQr(value)
      if (i && i.token) {
        done.current = true
        setScanning(false)
        onConnect(i, value)
      } else {
        setBad(true)
      }
    },
    [onConnect],
  )
  const lastInfo = settings.lastQr ? parseQr(settings.lastQr) : null

  return (
    <View style={styles.row}>
      <StatusBar hidden />
      <View style={styles.scanBox}>
        {scanning ? (
          Platform.OS === 'ios' ? <IosQrScanner onCode={onCode} /> : <SnapshotQrScanner onCode={onCode} />
        ) : null}
        <View style={styles.scanFrame} pointerEvents="none" />
        <Text style={styles.scanHint}>
          {bad ? "Ce n'est pas un QR code de MOCAM Studio" : 'Visez le QR code affiché par MOCAM Studio sur le PC'}
        </Text>
      </View>
      <View style={styles.side}>
        <RNImage source={require('./assets/logo-mocam.png')} style={styles.logo} resizeMode="contain" accessibilityLabel="MOCAM" />
        <Text style={styles.label}>Nom de cette caméra</Text>
        <TextInput
          style={styles.input}
          value={settings.name}
          autoCapitalize="none"
          autoCorrect={false}
          onChangeText={(t) => onSettings({ ...settings, name: t })}
          onEndEditing={() => onSettings({ ...settings, name: cleanName(settings.name) })}
        />
        <Text style={styles.small}>Il donne son nom à la vidéo : iphone, poco…</Text>
        <Text style={[styles.small, { marginTop: 18, color: '#aab' }]}>
          Le téléphone et le PC doivent être sur le même Wi-Fi. Sans Wi-Fi, activez le partage de connexion
          d'un des téléphones et connectez-y le PC : ce téléphone peut filmer lui aussi.
        </Text>
        {lastInfo ? (
          <Btn
            label="Reprendre la dernière connexion"
            onPress={() => {
              done.current = true
              onConnect(lastInfo, settings.lastQr!)
            }}
          />
        ) : null}
      </View>
    </View>
  )
}

/** iPhone : lecteur de codes natif de VisionCamera. */
function IosQrScanner({ onCode }: { onCode: (v: string) => void }) {
  const onScanned = useCallback(
    (objects: ScannedObject[]) => {
      for (const o of objects) {
        if (isScannedCode(o) && o.value) onCode(o.value)
      }
    },
    [onCode],
  )
  const objectOutput = useObjectOutput({ types: QR_TYPES, onObjectsScanned: onScanned })
  return <Camera style={StyleSheet.absoluteFill} device="back" isActive outputs={[objectOutput]} />
}

/** Android : VisionCamera n'y a pas de lecteur de codes, on décode les images de l'aperçu. */
function SnapshotQrScanner({ onCode }: { onCode: (v: string) => void }) {
  const ref = useRef<CameraRef>(null)
  const [ready, setReady] = useState(false)
  const onCodeRef = useRef(onCode)
  onCodeRef.current = onCode
  useEffect(() => {
    if (!ready) return
    let alive = true
    const loop = async () => {
      while (alive) {
        try {
          const cam = ref.current
          if (cam) {
            let img = await cam.takeSnapshot()
            const scale = 800 / Math.max(img.width, img.height)
            if (scale < 1) img = await img.resizeAsync(Math.round(img.width * scale), Math.round(img.height * scale))
            const raw = await img.toRawPixelDataAsync()
            const value = decodeQr(raw)
            if (value && alive) onCodeRef.current(value)
          }
        } catch {
          // aperçu pas encore prêt
        }
        await new Promise((r) => setTimeout(r, 200))
      }
    }
    void loop()
    return () => {
      alive = false
    }
  }, [ready])
  return (
    <Camera
      ref={ref}
      style={StyleSheet.absoluteFill}
      device="back"
      isActive
      onPreviewStarted={() => setReady(true)}
    />
  )
}

// ============================================================================ Capture

interface CamStatus {
  fps?: number
  stabilization?: string
  binned?: boolean
}

function CaptureScreen({
  info,
  settings,
  onSettings,
  onDisconnect,
}: {
  info: ConnectInfo
  settings: Settings
  onSettings: (s: Settings) => void
  onDisconnect: () => void
}) {
  useKeepAwake()
  // Objectif principal (×1) seul : sur iPhone, la caméra « par défaut » peut être une
  // caméra virtuelle (LiDAR + grand-angle, double…), qu'on évite.
  const defaultBack = useCameraDevice('back')
  const allDevices = useCameraDevices()
  // champ de vision, en équivalent zoom de l'objectif principal : de ×1 (objectif principal,
  // sans zoom) à ×0,5 environ (ultra grand-angle). On ne fait qu'élargir, jamais zoomer :
  // ×1 est le plus serré. Choisi sur le téléphone ou depuis Studio.
  const [fov, setFov] = useState(1)
  // caméra avant : capture faciale (onglet Visage de MOCAM Studio)
  const [facing, setFacing] = useState<'back' | 'front'>('back')
  // mode Visage (caméra avant) : la vidéo suit le téléphone (verticale s'il est vertical)
  const faceMode = facing === 'front'
  const [uiRot, setUiRot] = useState(0)
  const lensSetup = useMemo(() => {
    if (facing === 'front') {
      const front = allDevices.find((d) => d.position === 'front' && !d.isVirtualDevice) ??
        allDevices.find((d) => d.position === 'front')
      if (front) return { device: front, zoom: 1, available: false, used: 'front' as const, fov: 1, options: [1] }
    }
    const back = allDevices.filter((d) => d.position === 'back')
    const physical = (t: string) => back.find((d) => d.type === t && !d.isVirtualDevice)
    let wide = defaultBack
    if (Platform.OS === 'ios') {
      // objectif seul : la caméra « par défaut » de l'iPhone peut être virtuelle (LiDAR, double…)
      wide = physical('wide-angle') ?? back.find((d) => d.type === 'wide-angle') ?? defaultBack
    }
    // ultra grand-angle seul (iPhone, certains Android) : son champ par rapport à l'objectif
    // principal vient des focales (13 mm / 26 mm = ×0,5 ; 16 / 26 ≈ ×0,6), sinon ×0,5
    const ultraDev = physical('ultra-wide-angle') ?? back.find((d) => d.type === 'ultra-wide-angle')
    const ratio =
      ultraDev?.focalLength && wide?.focalLength && ultraDev.focalLength < wide.focalLength
        ? ultraDev.focalLength / wide.focalLength
        : 0.5
    // Android : l'ultra grand-angle peut aussi s'obtenir en zoomant sous ×1 sur la caméra principale
    const zoomOutDev = !ultraDev && Platform.OS === 'android'
      ? [defaultBack, ...back].find((d) => d && (d.minZoom ?? 1) < 0.95)
      : undefined
    const widest = ultraDev
      ? Math.max(0.3, Math.round(ratio * 10) / 10)
      : zoomOutDev
        ? Math.round(Math.max(0.3, zoomOutDev.minZoom) * 10) / 10
        : 1
    // de l'ultra grand-angle à ×1, par pas de 0,1 (5 choix au plus)
    let options: number[] = [widest]
    for (let v = Math.round(widest * 10) + 1; v <= 10; v++) options.push(v / 10)
    if (options.length > 5) options = options.filter((v) => v !== 0.9)
    if (options.length > 5) options = [options[0], ...options.slice(-4)]
    // le plus proche des choix possibles
    const want = options.reduce((b, v) => (Math.abs(v - fov) < Math.abs(b - fov) ? v : b), options[0])
    const available = options.length > 1
    if (want < 0.99 && ultraDev) {
      const z = Math.min(ultraDev.maxZoom ?? 4, Math.max(ultraDev.minZoom ?? 1, want / ratio))
      return { device: ultraDev, zoom: z, available, used: 'ultra' as const, fov: want, options }
    }
    if (want < 0.99 && zoomOutDev)
      return { device: zoomOutDev, zoom: Math.max(zoomOutDev.minZoom, want), available, used: 'ultra' as const, fov: want, options }
    return { device: wide, zoom: 1, available, used: 'wide' as const, fov: 1, options }
  }, [allDevices, defaultBack, fov, facing])
  const device = lensSetup.device

  // 1080p par défaut ; si le téléphone refuse 60 i/s en 1080p et que le PC demande
  // la priorité à la cadence, on essaie le 720p.
  const [res720, setRes720] = useState(false)
  const prefer60 = useRef(true)
  const tried720 = useRef(false)
  const videoOutput = useVideoOutput({
    // Android : vidéo 4:3 = tout le capteur, exactement l'image de l'aperçu (en 16:9 le
    // téléphone coupait le haut et le bas). iPhone : 16:9, identique à son aperçu.
    targetResolution: FULL_SENSOR
      ? res720 ? CommonResolutions.HD_4_3 : CommonResolutions.FHD_4_3
      : res720 ? CommonResolutions.HD_16_9 : CommonResolutions.FHD_16_9,
    enableAudio: true,
    fileType: 'mp4',
  })
  // iPhone : l'aperçu ne peut pas être capturé, on prend de petites photos silencieuses
  const photoOutput = usePhotoOutput({
    targetResolution: CommonResolutions.FHD_16_9,
    containerFormat: 'jpeg',
    quality: 0.8,
    qualityPrioritization: 'balanced',
  })
  const liveRef = useRef({ device, videoOutput, res720, lensSetup })
  liveRef.current = { device, videoOutput, res720, lensSetup }
  const outputs = useMemo(
    () => (Platform.OS === 'ios' ? [videoOutput, photoOutput] : [videoOutput]),
    [videoOutput, photoOutput],
  )
  const constraints = useMemo<Constraint[]>(
    () => [
      { fps: 60 },
      { videoStabilizationMode: 'off' },
      { videoDynamicRange: CommonDynamicRanges.ANY_SDR },
      { resolutionBias: videoOutput },
    ],
    [videoOutput],
  )
  const cameraRef = useRef<CameraRef>(null)
  const viewSize = useRef({ width: 1, height: 1 })
  const [conn, setConn] = useState<{ state: ConnState; detail?: string }>({ state: 'connecting' })
  const [camStatus, setCamStatus] = useState<CamStatus>({})
  const [recording, setRecording] = useState<{ take: string; since: number } | null>(null)
  const [upload, setUpload] = useState<{ take: string; pct: number } | null>(null)
  const [locked, setLocked] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)
  const [identify, setIdentify] = useState(false)
  const [now, setNow] = useState(Date.now())

  const nameRef = useRef(settings.name)
  nameRef.current = settings.name
  const recorderRef = useRef<Recorder | null>(null)
  const takeRef = useRef<string>('')
  const previewFps = useRef(3)
  const busySnap = useRef(false)
  const wantFull = useRef(false)   // image pleine résolution demandée pendant l'envoi d'un aperçu
  const lastSnapSize = useRef<{ w: number; h: number } | null>(null)
  // format de l'aperçu (largeur / hauteur), connu dès la première image envoyée au PC
  const previewAspectRef = useRef(0)
  const [previewAspect, setPreviewAspect] = useState(0)
  const [layout, setLayout] = useState({ width: 0, height: 0 })
  const clientRef = useRef<StudioClient | null>(null)
  const camStatusRef = useRef<CamStatus>({})
  camStatusRef.current = camStatus
  const lockedRef = useRef(false)
  lockedRef.current = locked

  const flash = (msg: string) => {
    setBanner(msg)
    setTimeout(() => setBanner((b) => (b === msg ? null : b)), 4000)
  }

  // ---------------------------------------------------------------- envoi de la vidéo
  const uploadFile = useCallback(async (filePath: string, take: string) => {
    const client = clientRef.current
    if (!client?.baseUrl) {
      flash("Pas de connexion : la vidéo reste sur le téléphone")
      return
    }
    const uri = filePath.startsWith('file://') ? filePath : `file://${filePath}`
    const file = new File(uri)
    const url =
      `${client.baseUrl}/upload?token=${encodeURIComponent(client.token)}` +
      `&cam=${encodeURIComponent(nameRef.current)}&take=${encodeURIComponent(take)}&ext=mp4`
    let lastSent = 0
    const task = file.createUploadTask(url, {
      httpMethod: 'POST',
      uploadType: UploadType.BINARY_CONTENT,
      headers: { 'Content-Type': 'video/mp4' },
    })
    const sub = task.addListener('progress', (ev) => {
      const bytesSent = Number(ev.bytesSent)
      const totalBytes = Number(ev.totalBytes)
      const t = Date.now()
      if (t - lastSent > 300 || bytesSent === totalBytes) {
        lastSent = t
        setUpload({ take, pct: totalBytes ? bytesSent / totalBytes : 0 })
        client.send({ t: 'upload', take, sent: bytesSent, total: totalBytes })
      }
    })
    try {
      const res = await task.uploadAsync()
      if (res.status === 200) {
        removePending(take)
        client.send({ t: 'uploaded', take })
        try {
          file.delete()
        } catch {
          // pas grave
        }
        flash('Vidéo envoyée au PC')
      } else {
        client.send({ t: 'upload_error', take, message: `HTTP ${res.status}` })
        flash(`Erreur d'envoi (HTTP ${res.status})`)
      }
    } catch (e) {
      client.send({ t: 'upload_error', take, message: String(e) })
      flash("Erreur d'envoi : la vidéo reste sur le téléphone")
    } finally {
      sub.remove()
      setUpload(null)
    }
  }, [])

  // ---------------------------------------------------------------- enregistrement
  const arm = useCallback(async () => {
    if (recorderRef.current && !recorderRef.current.isRecording) return recorderRef.current
    recorderRef.current = await videoOutput.createRecorder({})
    return recorderRef.current
  }, [videoOutput])

  const startAt = useCallback(
    async (take: string, at: number) => {
      const client = clientRef.current
      try {
        if (recorderRef.current?.isRecording) {
          client?.send({ t: 'error', message: 'déjà en enregistrement' })
          return
        }
        const rec = await arm()
        takeRef.current = take
        const wait = at - Date.now()
        if (wait > 0) await new Promise((r) => setTimeout(r, wait))
        const called = Date.now()
        await rec.startRecording(
          (filePath) => {
            setRecording(null)
            recorderRef.current = null
            addPending({ take, path: filePath, cam: nameRef.current })
            try {
              client?.send({
                t: 'recorded',
                take,
                duration: Number(rec.recordedDuration),
                size: Number(rec.recordedFileSize),
              })
            } finally {
              void uploadFile(filePath, take)
            }
          },
          (error) => {
            setRecording(null)
            recorderRef.current = null
            client?.send({ t: 'error', message: `enregistrement : ${error.message}` })
            flash(`Erreur d'enregistrement : ${error.message}`)
          },
        )
        const started = Date.now()
        setRecording({ take, since: started })
        client?.send({ t: 'recording', take, called_at: called, started_at: started })
      } catch (e) {
        client?.send({ t: 'error', message: `démarrage : ${String(e)}` })
        flash(`Impossible de démarrer : ${String(e)}`)
      }
    },
    [arm, uploadFile],
  )

  const stop = useCallback(async () => {
    const rec = recorderRef.current
    if (rec?.isRecording) {
      try {
        await rec.stopRecording()
      } catch (e) {
        clientRef.current?.send({ t: 'error', message: `arrêt : ${String(e)}` })
      }
    }
  }, [])

  // ---------------------------------------------------------------- réglages d'image
  const lock = useCallback(async (fastShutter: boolean) => {
    const ref = cameraRef.current
    const client = clientRef.current
    if (!ref) return
    try {
      const { width, height } = viewSize.current
      await ref.focusTo(
        { x: width / 2, y: height / 2 },
        { adaptiveness: 'locked', autoResetAfter: null, responsiveness: 'snappy' },
      )
      const c = ref.controller
      if (fastShutter && Platform.OS === 'ios' && c && c.device.supportsExposureLocking) {
        // temps de pose court (1/500 s) pour éviter le flou de bougé, ISO compensé
        await new Promise((r) => setTimeout(r, 600))
        const d = c.exposureDuration
        const iso = c.iso
        const target = Math.max(c.minExposureDuration, Math.min(1 / 500, d))
        const newIso = Math.min(c.maxISO, Math.max(c.minISO, (iso * d) / target))
        await c.setExposureLocked(target, newIso)
      }
      setLocked(true)
      client?.send({ t: 'locked', ok: true })
    } catch (e) {
      client?.send({ t: 'locked', ok: false, message: String(e) })
      flash(`Blocage impossible : ${String(e)}`)
    }
  }, [])

  const unlock = useCallback(async () => {
    try {
      await cameraRef.current?.resetFocus()
    } catch {
      // ignore
    }
    setLocked(false)
    clientRef.current?.send({ t: 'locked', ok: false })
  }, [])

  const applyPrefer60 = useCallback(
    (v: boolean) => {
      prefer60.current = v
      if (recorderRef.current?.isRecording) return
      if (!v && res720) setRes720(false)
      if (v && !res720 && (camStatusRef.current.fps ?? 60) < 50) {
        tried720.current = true
        setRes720(true)
      }
    },
    [res720],
  )

  // ---------------------------------------------------------------- images vers le PC
  const sendSnapshot = useCallback(async (kind: 1 | 2) => {
    const ref = cameraRef.current
    const client = clientRef.current
    if (!ref || !client) return
    if (busySnap.current) {
      // un aperçu est en cours d'envoi : l'image de vérification des boules partira juste après
      if (kind === 2) wantFull.current = true
      return
    }
    busySnap.current = true
    try {
      let img: Image
      if (Platform.OS === 'ios') {
        const photo = await photoOutput.capturePhoto({ enableShutterSound: false, flashMode: 'off' }, {})
        try {
          img = await photo.toImageAsync()
        } finally {
          photo.dispose()
        }
      } else {
        img = await ref.takeSnapshot()
      }
      // le redimensionnement applique aussi l'orientation (images toujours « à l'endroit »)
      const maxSide = kind === 1 ? 640 : 1920
      const scale = Math.min(1, maxSide / Math.max(img.width, img.height))
      img = await img.resizeAsync(Math.round(img.width * scale), Math.round(img.height * scale))
      lastSnapSize.current = { w: img.width, h: img.height }
      const pa = Math.max(img.width, img.height) / Math.min(img.width, img.height)
      if (Math.abs(pa - previewAspectRef.current) > 0.01) {
        previewAspectRef.current = pa
        setPreviewAspect(pa)
      }
      // même cadrage que la vidéo enregistrée (l'aperçu peut être plus haut ou plus large)
      img = await cropToAspect(img, videoAspect(videoOutput.currentResolution))
      const enc = await img.toEncodedImageDataAsync('jpg', kind === 1 ? 55 : 92)
      client.sendBinary(kind, enc.buffer)
    } catch {
      // caméra pas prête
    } finally {
      busySnap.current = false
      if (wantFull.current) {
        wantFull.current = false
        setTimeout(() => void sendSnapshotRef.current(2), 0)
      }
    }
  }, [photoOutput, videoOutput])
  const sendSnapshotRef = useRef(sendSnapshot)
  sendSnapshotRef.current = sendSnapshot

  // ---------------------------------------------------------------- commandes du PC
  const onCommand = useCallback(
    (cmd: StudioCommand) => {
      switch (cmd.t) {
        case 'welcome':
          for (const p of getPending()) {
            try {
              const uri = p.path.startsWith('file://') ? p.path : `file://${p.path}`
              if (new File(uri).exists) void uploadFile(p.path, p.take)
              else removePending(p.take)
            } catch {
              removePending(p.take)
            }
          }
          if (cmd.name && cmd.name !== nameRef.current) onSettings({ ...settings, name: cmd.name })
          if (typeof cmd.prefer60 === 'boolean') applyPrefer60(cmd.prefer60)
          if (typeof cmd.preview === 'number') previewFps.current = cmd.preview
          if (typeof cmd.fov === 'number') setFov(cmd.fov)
          else if (cmd.lens === 'wide' || cmd.lens === 'ultra') setFov(cmd.lens === 'ultra' ? 0.5 : 1)
          break
        case 'arm':
          void arm()
          break
        case 'start':
          void startAt(cmd.take, cmd.at)
          break
        case 'stop':
          void stop()
          break
        case 'lock':
          void lock(!!cmd.fast_shutter)
          break
        case 'unlock':
          void unlock()
          break
        case 'snapshot':
          void sendSnapshot(2)
          break
        case 'preview':
          previewFps.current = cmd.fps
          break
        case 'config':
          if (!recorderRef.current?.isRecording) {
            if (cmd.facing === 'front' || cmd.facing === 'back') setFacing(cmd.facing)
            if (typeof cmd.fov === 'number') setFov(cmd.fov)
            else if (cmd.lens === 'wide' || cmd.lens === 'ultra') setFov(cmd.lens === 'ultra' ? 0.5 : 1)
          }
          if (typeof cmd.prefer60 === 'boolean') applyPrefer60(cmd.prefer60)
          if (typeof cmd.preview === 'number') previewFps.current = cmd.preview
          break
        case 'identify':
          setIdentify(true)
          setTimeout(() => setIdentify(false), 3000)
          break
        case 'rename':
          onSettings({ ...settings, name: cleanName(cmd.name) })
          break
        case 'error':
          flash(cmd.message)
          break
      }
    },
    [arm, startAt, stop, lock, unlock, sendSnapshot, onSettings, settings, applyPrefer60, uploadFile],
  )
  const onCommandRef = useRef(onCommand)
  onCommandRef.current = onCommand

  // erreurs inattendues : bandeau + signalement au PC, sans couper la caméra ni la connexion
  useEffect(() => {
    const l = (e: Error) => {
      flash(`Erreur : ${e?.message ?? String(e)}`)
      clientRef.current?.send({ t: 'error', message: `app : ${e?.message ?? String(e)}` })
    }
    errorListeners.add(l)
    return () => {
      errorListeners.delete(l)
    }
  }, [])

  // ---------------------------------------------------------------- connexion
  useEffect(() => {
    const client = new StudioClient(
      info,
      () => ({
        name: nameRef.current,
        platform: Platform.OS,
        model: (Platform.constants as any)?.Model ?? (Platform.constants as any)?.model ?? '',
        os: String(Platform.Version),
        app: APP_VERSION,
      }),
      (cmd) => onCommandRef.current(cmd),
      (state, detail) => setConn({ state, detail }),
    )
    clientRef.current = client
    client.start()
    return () => {
      client.stop()
      clientRef.current = null
    }
  }, [info])

  // état envoyé chaque seconde + horloge de l'écran
  useEffect(() => {
    const id = setInterval(async () => {
      setNow(Date.now())
      const client = clientRef.current
      if (!client) return
      try {
      let battery = -1
      try {
        battery = await Battery.getBatteryLevelAsync()
      } catch {
        // indisponible
      }
      const c = cameraRef.current?.controller
      const rec = recorderRef.current
      client.send({
        t: 'status',
        name: nameRef.current,
        battery,
        storage_free: (() => {
          try {
            return Paths.availableDiskSpace
          } catch {
            return null
          }
        })(),
        fps: camStatusRef.current.fps ?? null,
        stabilization: camStatusRef.current.stabilization ?? null,
        binned: camStatusRef.current.binned ?? null,
        snapshot_size: lastSnapSize.current,
        exposure: c
          ? { duration: c.exposureDuration, iso: c.iso, mode: c.exposureMode, focus: c.focusMode }
          : null,
        locked: lockedRef.current,
        recording: !!rec?.isRecording,
        recorded_duration: rec?.isRecording ? rec.recordedDuration : 0,
        take: rec?.isRecording ? takeRef.current : null,
        device: c?.device.localizedName ?? liveRef.current.device?.localizedName ?? null,
        device_type: liveRef.current.device?.type ?? null,
        resolution: liveRef.current.videoOutput.currentResolution ?? null,
        res720: liveRef.current.res720,
        lens: liveRef.current.lensSetup.used,
        ultra_available: liveRef.current.lensSetup.available,
        zoom: liveRef.current.lensSetup.zoom,
        fov: liveRef.current.lensSetup.fov,
        fov_options: liveRef.current.lensSetup.options,
        facing: liveRef.current.lensSetup.used === 'front' ? 'front' : 'back',
        app: APP_VERSION,
        supports60: (() => {
          try {
            return liveRef.current.device ? liveRef.current.device.supportsFPS(60) : null
          } catch {
            return null
          }
        })(),
      })
      } catch (e) {
        client.send({ t: 'error', message: `état : ${String(e)}` })
      }
    }, 1000)
    return () => clearInterval(id)
  }, [])

  // aperçu envoyé au PC
  useEffect(() => {
    let alive = true
    const loop = async () => {
      while (alive) {
        const rec = !!recorderRef.current?.isRecording
        const fps = rec ? (Platform.OS === 'ios' ? 0 : Math.min(previewFps.current, 1)) : previewFps.current
        if (fps > 0 && clientRef.current?.baseUrl) await sendSnapshot(1)
        await new Promise((r) => setTimeout(r, fps > 0 ? 1000 / fps : 500))
      }
    }
    void loop()
    return () => {
      alive = false
    }
  }, [sendSnapshot])

  const onConfig = useCallback(
    (c: CameraSessionConfig) => {
      setCamStatus({
        fps: c.selectedFPS,
        stabilization: c.selectedVideoStabilizationMode,
        binned: c.isBinned,
      })
      if (recorderRef.current?.isRecording) return
      const fps = c.selectedFPS ?? 0
      if (fps > 0 && fps < 50 && prefer60.current && !res720 && !tried720.current) {
        tried720.current = true
        setRes720(true)
      } else if (fps > 0 && fps < 50 && res720) {
        setRes720(false) // le 720p ne donne pas mieux : retour au 1080p
      }
    },
    [res720],
  )



  const onLayout = (e: LayoutChangeEvent) => {
    viewSize.current = { width: e.nativeEvent.layout.width, height: e.nativeEvent.layout.height }
    setLayout(viewSize.current)
  }
  // Android : si l'aperçu n'a pas le format de la vidéo, on assombrit ce qui ne sera pas filmé
  const guide =
    Platform.OS === 'android'
      ? recordedArea(layout, previewAspect, videoAspect(videoOutput.currentResolution))
      : null

  const recSecs = recording ? Math.max(0, (now - recording.since) / 1000) : 0
  const connColor = conn.state === 'connected' ? '#3ddc84' : conn.state === 'error' ? '#ff5252' : '#ffb300'

  return (
    <View style={styles.full} onLayout={onLayout}>
      <StatusBar hidden />
      {device ? (
        <Camera
          ref={cameraRef}
          style={StyleSheet.absoluteFill}
          device={device}
          isActive
          outputs={outputs}
          constraints={constraints}
          zoom={lensSetup.zoom}
          resizeMode="contain"
          enableDistortionCorrection={device.supportsDistortionCorrection ? true : undefined}
          orientationSource={faceMode ? 'device' : 'interface'}
          onUIRotationChanged={setUiRot}
          mirrorMode="off"
          onSessionConfigSelected={onConfig}
          onError={(e) => flash(`Caméra : ${e.message}`)}
        />
      ) : (
        <View style={styles.center}>
          <ActivityIndicator color="#fff" />
        </View>
      )}

      {guide
        ? guide.masks.map((m, i) => <View key={i} pointerEvents="none" style={[styles.mask, m]} />)
        : null}
      {guide ? <View pointerEvents="none" style={[styles.frameGuide, guide.frame]} /> : null}

      <View style={styles.topBar}>
        <View style={[styles.dot, { backgroundColor: connColor }]} />
        <Text style={styles.camName}>{settings.name}</Text>
        <Text style={styles.small}>
          {conn.state === 'connected' ? 'connecté à MOCAM Studio' : conn.detail ?? 'connexion…'}
        </Text>
        <View style={{ flex: 1 }} />
        {recording ? (
          <View style={styles.rec}>
            <View style={styles.recDot} />
            <Text style={styles.recText}>REC {recSecs.toFixed(0)} s</Text>
          </View>
        ) : null}
      </View>

      <View style={styles.bottomBar}>
        {lensSetup.options.length < 2 ? (
          <Text style={styles.small}>{lensSetup.used === 'front' ? 'Caméra avant : capture du visage' : "×1 : ce téléphone ne laisse pas l'app élargir le champ"}</Text>
        ) : null}
        <View style={[styles.seg, lensSetup.options.length < 2 ? { display: 'none' } : null]}>
          {lensSetup.options.map((v) => {
            const on = Math.abs(v - lensSetup.fov) < 0.01
            return (
              <Pressable
                key={v}
                disabled={!!recording}
                onPress={() => setFov(v)}
                style={[styles.segBtn, on && styles.segOn, recording ? { opacity: 0.4 } : null]}
              >
                <Text style={[styles.segText, on && styles.segTextOn]}>{`×${String(v).replace('.', ',')}`}</Text>
              </Pressable>
            )
          })}
        </View>
        <Text style={styles.small}>
          {camStatus.fps ? `${camStatus.fps} i/s` : '… i/s'}
          {locked ? '   exposition bloquée' : ''}
        </Text>
        <View style={{ flex: 1 }} />
        {upload ? <Text style={styles.small}>Envoi {Math.round(upload.pct * 100)} %</Text> : null}
        <Pressable
          disabled={!!recording}
          onPress={() => setFacing(faceMode ? 'back' : 'front')}
          style={[styles.smallBtn, faceMode ? styles.faceOn : null, recording ? { opacity: 0.4 } : null]}
        >
          <Text style={styles.smallBtnText}>{faceMode ? 'Visage : oui' : 'Visage'}</Text>
        </Pressable>
        <Pressable
          style={styles.smallBtn}
          onPress={() => {
            void stop()
            onDisconnect()
          }}
        >
          <Text style={styles.smallBtnText}>Déconnecter</Text>
        </Pressable>
      </View>

      {faceMode ? (
        <View style={styles.faceRecWrap} pointerEvents="box-none">
          <Pressable
            accessibilityLabel={recording ? "Arrêter l'enregistrement du visage" : 'Enregistrer le visage'}
            disabled={conn.state !== 'connected' || (!recording && !!upload)}
            onPress={() => clientRef.current?.send({ t: 'face_record' })}
            style={[styles.faceRec, conn.state !== 'connected' ? { opacity: 0.4 } : null,
              { transform: [{ rotate: `${uiRot}deg` }] }]}
          >
            <View style={recording ? styles.faceRecStop : styles.faceRecDot} />
          </Pressable>
          <Text style={[styles.small, { transform: [{ rotate: `${uiRot}deg` }] }]}>
            {conn.state !== 'connected' ? 'Non connecté' : recording ? 'Arrêter' : upload ? 'Envoi…' : 'REC visage'}
          </Text>
        </View>
      ) : null}
      {banner ? (
        <View style={styles.banner}>
          <Text style={styles.text}>{banner}</Text>
        </View>
      ) : null}
      {identify ? (
        <View style={styles.identify}>
          <Text style={styles.identifyText}>{settings.name}</Text>
        </View>
      ) : null}
    </View>
  )
}

// ============================================================================ cadrage

/** Android : vidéo en 4:3 (tout le capteur, comme l'aperçu). */
const FULL_SENSOR = Platform.OS === 'android'

/** Format (grand côté / petit côté) de la vidéo ; 16:9 ou 4:3 par défaut. */
function videoAspect(res?: { width: number; height: number } | null): number {
  if (res && res.width > 0 && res.height > 0) return Math.max(res.width, res.height) / Math.min(res.width, res.height)
  return FULL_SENSOR ? 4 / 3 : 16 / 9
}

/** Recadre une image (déjà à l'endroit) au format de la vidéo, au centre. */
async function cropToAspect(img: Image, aspect: number): Promise<Image> {
  const landscape = img.width >= img.height
  const long = landscape ? img.width : img.height
  const short = landscape ? img.height : img.width
  const ia = long / short
  if (Math.abs(ia - aspect) / aspect < 0.02) return img
  let w = img.width
  let h = img.height
  if (ia < aspect) {
    // image plus « carrée » que la vidéo : on retire des bandes sur le petit côté
    if (landscape) h = Math.round(img.width / aspect)
    else w = Math.round(img.height / aspect)
  } else if (landscape) w = Math.round(img.height * aspect)
  else h = Math.round(img.width * aspect)
  const x = Math.round((img.width - w) / 2)
  const y = Math.round((img.height - h) / 2)
  return img.cropAsync(x, y, x + w, y + h)
}

type Rect = { left: number; top: number; width: number; height: number }

/** Zone réellement filmée dans l'aperçu affiché en « contain », et bandes à assombrir. */
function recordedArea(
  view: { width: number; height: number },
  previewAspect: number,
  vAspect: number,
): { frame: Rect; masks: Rect[] } | null {
  if (!view.width || !view.height || !previewAspect || Math.abs(previewAspect - vAspect) / vAspect < 0.02) return null
  const landscape = view.width >= view.height
  // aperçu affiché en entier, centré
  const pa = landscape ? previewAspect : 1 / previewAspect
  let pw = view.width
  let ph = pw / pa
  if (ph > view.height) {
    ph = view.height
    pw = ph * pa
  }
  const px = (view.width - pw) / 2
  const py = (view.height - ph) / 2
  const va = landscape ? vAspect : 1 / vAspect
  let fw = pw
  let fh = fw / va
  if (fh > ph) {
    fh = ph
    fw = fh * va
  }
  const frame = { left: px + (pw - fw) / 2, top: py + (ph - fh) / 2, width: fw, height: fh }
  const masks: Rect[] =
    fw < pw - 1
      ? [
          { left: px, top: py, width: frame.left - px, height: ph },
          { left: frame.left + fw, top: py, width: px + pw - frame.left - fw, height: ph },
        ]
      : [
          { left: px, top: py, width: pw, height: frame.top - py },
          { left: px, top: frame.top + fh, width: pw, height: py + ph - frame.top - fh },
        ]
  return { frame, masks }
}

// ============================================================================ UI

function Btn({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable style={({ pressed }) => [styles.btn, pressed && { opacity: 0.7 }]} onPress={onPress}>
      <Text style={styles.btnText}>{label}</Text>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  full: { flex: 1, backgroundColor: '#000' },
  row: { flex: 1, flexDirection: 'row', backgroundColor: '#111' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: '#111', padding: 24 },
  scanBox: { flex: 3, backgroundColor: '#000', alignItems: 'center', justifyContent: 'center' },
  scanFrame: { width: 220, height: 220, borderWidth: 3, borderColor: '#fff', borderRadius: 16, opacity: 0.8 },
  scanHint: { position: 'absolute', bottom: 16, color: '#fff', fontSize: 15, backgroundColor: '#0008', padding: 6 },
  side: { flex: 2, padding: 20, justifyContent: 'center' },
  title: { color: '#fff', fontSize: 28, fontWeight: '700', marginBottom: 12 },
  logo: { width: 124, height: 64, marginBottom: 14 },
  label: { color: '#bbb', fontSize: 14, marginBottom: 4 },
  input: {
    color: '#fff',
    fontSize: 18,
    borderWidth: 1,
    borderColor: '#555',
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 6,
  },
  text: { color: '#fff', fontSize: 16, textAlign: 'center', marginBottom: 16 },
  small: { color: '#ddd', fontSize: 13 },
  btn: { backgroundColor: '#2f6fed', borderRadius: 10, paddingVertical: 12, paddingHorizontal: 18, marginTop: 10, alignItems: 'center' },
  btnText: { color: '#fff', fontSize: 16, fontWeight: '600' },
  topBar: {
    position: 'absolute', top: 0, left: 0, right: 0, flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 16, paddingVertical: 8, backgroundColor: '#0009', gap: 10,
  },
  bottomBar: {
    position: 'absolute', bottom: 0, left: 0, right: 0, flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 16, paddingVertical: 6, backgroundColor: '#0009', gap: 12,
  },
  dot: { width: 12, height: 12, borderRadius: 6 },
  camName: { color: '#fff', fontSize: 20, fontWeight: '700' },
  rec: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  recDot: { width: 14, height: 14, borderRadius: 7, backgroundColor: '#ff3b30' },
  faceOn: { borderColor: '#3ddc84', backgroundColor: '#3ddc8433' },
  faceRecWrap: { position: 'absolute', right: 24, top: 0, bottom: 0, justifyContent: 'center', alignItems: 'center', gap: 8 },
  faceRec: {
    width: 76, height: 76, borderRadius: 38, borderWidth: 4, borderColor: '#fff',
    alignItems: 'center', justifyContent: 'center', backgroundColor: '#0006',
  },
  faceRecDot: { width: 56, height: 56, borderRadius: 28, backgroundColor: '#ff3b30' },
  faceRecStop: { width: 30, height: 30, borderRadius: 6, backgroundColor: '#ff3b30' },
  recText: { color: '#ff3b30', fontSize: 18, fontWeight: '700' },
  smallBtn: { borderWidth: 1, borderColor: '#aaa', borderRadius: 6, paddingHorizontal: 10, paddingVertical: 4 },
  smallBtnText: { color: '#fff', fontSize: 13 },
  banner: { position: 'absolute', top: 60, alignSelf: 'center', backgroundColor: '#000c', borderRadius: 8, padding: 10 },
  identify: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: '#2f6fedcc', alignItems: 'center', justifyContent: 'center' },
  identifyText: { color: '#fff', fontSize: 96, fontWeight: '800' },
  mask: { position: 'absolute', backgroundColor: '#000a' },
  seg: { flexDirection: 'row', backgroundColor: '#000a', borderRadius: 18, padding: 3, gap: 2 },
  segBtn: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 15 },
  segOn: { backgroundColor: '#ffd54f' },
  segText: { color: '#fff', fontSize: 15, fontWeight: '600' },
  segTextOn: { color: '#111' },
  frameGuide: { position: 'absolute', borderWidth: 2, borderColor: '#ffd54f' },
})
