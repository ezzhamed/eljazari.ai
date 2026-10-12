'use client'

/** Telemetry-driven flight scene with live monitoring and recording playback. */
import { useMemo, Suspense, useState, useEffect } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { OrbitControls, Text, Sky, Line } from '@react-three/drei'
import * as THREE from 'three'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import {
  Video, Eye, Compass, Sun, Moon, Play, Pause, RotateCcw, BarChart2,
  Orbit, Maximize2, Minimize2, Radio, ArrowUp, Gauge, Battery, Navigation, X,
} from 'lucide-react'
import { useFlightPath, WaypointInfo } from '@/lib/hooks/useFlightPath'
import { ParametricDroneMesh } from './DroneViewer'
import styles from './FlightPathViewer.module.css'

type CameraMode = 'chase' | 'orbit' | 'fpv'
type EnvTheme = 'day' | 'night'

// ─────────────────────────────────────────────────────────────
// Airfield, Runway & Distance Rings
// ─────────────────────────────────────────────────────────────

function AirfieldGround({ theme = 'day' }: { theme: EnvTheme }) {
  const isDay = theme === 'day'
  const groundColor = isDay ? '#18241b' : '#0b121c'
  const gridColor = isDay ? '#2a3d2e' : '#142230'
  const runwayColor = isDay ? '#222629' : '#0e141a'
  const ringColor = isDay ? '#3b5440' : '#1e384d'
  const markColor = isDay ? '#e5e7eb' : '#38bdf8'

  return (
    <group position={[0, -0.01, 0]}>
      {/* Endless terrain plane */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <planeGeometry args={[200, 200]} />
        <meshStandardMaterial color={groundColor} roughness={0.9} metalness={0.1} />
      </mesh>

      <gridHelper args={[80, 80, gridColor, gridColor]} position={[0, 0.008, 0]} />
      {/* Runway Strip */}
      <mesh position={[0, 0.002, 0]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <planeGeometry args={[1.4, 14]} />
        <meshStandardMaterial color={runwayColor} roughness={0.7} />
      </mesh>

      {/* Runway Centerline Dashes */}
      {[-5, -3, -1, 1, 3, 5].map((z) => (
        <mesh key={z} position={[0, 0.004, z]} rotation={[-Math.PI / 2, 0, 0]}>
          <planeGeometry args={[0.08, 1.0]} />
          <meshBasicMaterial color={markColor} opacity={0.85} transparent />
        </mesh>
      ))}

      {/* Threshold markings at runway ends */}
      {[-6.2, 6.2].map((z) => (
        <group key={z} position={[0, 0.004, z]}>
          {[-0.4, -0.2, 0, 0.2, 0.4].map((x) => (
            <mesh key={x} position={[x, 0, 0]} rotation={[-Math.PI / 2, 0, 0]}>
              <planeGeometry args={[0.06, 0.6]} />
              <meshBasicMaterial color={markColor} opacity={0.9} transparent />
            </mesh>
          ))}
        </group>
      ))}

      {/* Takeoff / Landing Pad at Origin */}
      <group position={[0, 0.005, 0]}>
        <mesh rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[0.8, 0.95, 36]} />
          <meshBasicMaterial color={markColor} opacity={0.8} transparent />
        </mesh>
        <mesh rotation={[-Math.PI / 2, 0, 0]}>
          <circleGeometry args={[0.78, 36]} />
          <meshBasicMaterial color={isDay ? '#1e2822' : '#0b1622'} opacity={0.85} transparent />
        </mesh>
        <Text
          position={[0, 0.01, 0]}
          rotation={[-Math.PI / 2, 0, 0]}
          fontSize={0.45}
          color={markColor}
          anchorX="center"
          anchorY="middle"
          font={undefined}
        >
          H
        </Text>
      </group>

      {/* Tactical Concentric Range Rings */}
      {[5, 10, 20, 35].map((r) => (
        <group key={r}>
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.001, 0]}>
            <ringGeometry args={[r - 0.03, r + 0.03, 72]} />
            <meshBasicMaterial color={ringColor} opacity={0.4} transparent />
          </mesh>
        </group>
      ))}
    </group>
  )
}

// ─────────────────────────────────────────────────────────────
// Drone with Navigation Lights & Altitude Shadow Projection
// ─────────────────────────────────────────────────────────────

function TravellingDrone({
  position,
  rotation,
  theme = 'day',
}: {
  position: THREE.Vector3
  rotation: THREE.Euler
  theme: EnvTheme
}) {
  const isDay = theme === 'day'

  // Height above ground
  const alt = Math.max(0, position.y)

  return (
    <group>
      {/* ── Ground shadow & altitude drop-line ────────────────── */}
      <group position={[position.x, 0.006, position.z]}>
        {/* Ground track shadow spot */}
        <mesh rotation={[-Math.PI / 2, 0, 0]}>
          <circleGeometry args={[Math.max(0.2, 0.6 - alt * 0.03), 20]} />
          <meshBasicMaterial
            color="#000000"
            transparent
            opacity={Math.max(0.15, 0.65 - alt * 0.05)}
          />
        </mesh>
        {/* Ground target reticle */}
        <mesh rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[0.25, 0.28, 24]} />
          <meshBasicMaterial
            color={isDay ? '#38bdf8' : '#00ffcc'}
            transparent
            opacity={0.5}
          />
        </mesh>
      </group>

      {/* Altitude vertical laser drop-line connecting aircraft to ground */}
      {alt > 0.1 && (
        <line>
          <bufferGeometry
            attach="geometry"
            onUpdate={(self) => {
              const pts = [
                new THREE.Vector3(position.x, 0.006, position.z),
                new THREE.Vector3(position.x, position.y, position.z),
              ]
              self.setFromPoints(pts)
            }}
          />
          <lineDashedMaterial
            attach="material"
            color={isDay ? '#0284c7' : '#00e5ff'}
            dashSize={0.15}
            gapSize={0.1}
            opacity={0.45}
            transparent
          />
        </line>
      )}

      {/* ── 3D Aircraft Model ─────────────────────────────────── */}
      <group position={position} rotation={rotation} scale={0.75}>
        <ParametricDroneMesh
          viewMode="solid"
          propSpeed={22}
          animateProps
          selectedComponent={null}
          onSelect={() => {}}
          armScaleOverride={1}
        />

        {/* Navigation lights: Red Port (Left Wing), Green Starboard (Right Wing) */}
        <group position={[-0.85, 0.02, 0.06]}>
          <mesh>
            <sphereGeometry args={[0.035, 8, 8]} />
            <meshBasicMaterial color="#ef4444" />
          </mesh>
          <pointLight color="#ef4444" intensity={1.5} distance={1.2} />
        </group>

        <group position={[0.85, 0.02, 0.06]}>
          <mesh>
            <sphereGeometry args={[0.035, 8, 8]} />
            <meshBasicMaterial color="#22c55e" />
          </mesh>
          <pointLight color="#22c55e" intensity={1.5} distance={1.2} />
        </group>

        {/* Tail white strobe */}
        <group position={[0, 0.18, -0.45]}>
          <mesh>
            <sphereGeometry args={[0.025, 8, 8]} />
            <meshBasicMaterial color="#ffffff" />
          </mesh>
          <pointLight color="#ffffff" intensity={2.0} distance={1.5} />
        </group>
      </group>
    </group>
  )
}

// ─────────────────────────────────────────────────────────────
// Glowing Flight Path Ribbon
// ─────────────────────────────────────────────────────────────

function SmoothPath({ points, isDay }: { points: THREE.Vector3[]; isDay: boolean }) {
  const tube = useMemo(() => {
    if (points.length < 2) return null
    const curve = new THREE.CatmullRomCurve3(points)
    return new THREE.TubeGeometry(curve, Math.max(points.length * 3, 24), 0.018, 8, false)
  }, [points])

  useEffect(() => () => tube?.dispose(), [tube])

  if (!tube) return null

  const pathColor = isDay ? '#d97706' : '#38bdf8'
  const emissiveColor = isDay ? '#f59e0b' : '#0284c7'

  return (
    <mesh>
      <primitive object={tube} attach="geometry" />
      <meshStandardMaterial
        color={pathColor}
        emissive={emissiveColor}
        emissiveIntensity={0.65}
        transparent
        opacity={0.85}
        metalness={0.3}
        roughness={0.4}
      />
    </mesh>
  )
}

// ─────────────────────────────────────────────────────────────
// Waypoint Rings with Hover Tooltip
// ─────────────────────────────────────────────────────────────

function WaypointSpheres({
  infos,
  currentIndex,
  onHover,
}: {
  infos: WaypointInfo[]
  currentIndex: number
  onHover: (info: WaypointInfo | null) => void
}) {
  const stride = Math.max(1, Math.floor(infos.length / 25))

  return (
    <>
      {infos
        .filter((_, i) => i % stride === 0 || i === 0 || i === infos.length - 1)
        .map((info) => {
          const isCurrent = Math.abs(info.index - currentIndex) < stride
          const isLast = info.index === infos.length - 1
          return (
            <group key={info.index} position={info.position}>
              <mesh
                onPointerOver={(e) => {
                  e.stopPropagation()
                  onHover(info)
                }}
                onPointerOut={() => onHover(null)}
              >
                <sphereGeometry args={[isCurrent || isLast ? 0.07 : 0.035, 12, 12]} />
                <meshStandardMaterial
                  color={isLast ? '#ef4444' : isCurrent ? '#38bdf8' : '#eab308'}
                  emissive={isCurrent ? '#38bdf8' : '#ca8a04'}
                  emissiveIntensity={isCurrent ? 0.9 : 0.4}
                />
              </mesh>
            </group>
          )
        })}
    </>
  )
}

// ─────────────────────────────────────────────────────────────
// Dynamic Camera Controller (Chase Cam / FPV / Orbit)
// ─────────────────────────────────────────────────────────────

function DynamicCameraController({
  cameraMode,
  dronePosition,
  droneRotation,
}: {
  cameraMode: CameraMode
  dronePosition: THREE.Vector3
  droneRotation: THREE.Euler
}) {
  const { camera } = useThree()
  useEffect(() => {
    if (cameraMode === 'orbit') {
      camera.position.set(10, 9, 10)
      camera.lookAt(0, 1, 0)
    }
  }, [cameraMode, camera])

  useFrame(() => {
    if (cameraMode === 'chase') {
      const yaw = droneRotation.y
      // Position camera behind and slightly above the drone
      const dist = 3.8
      const height = 1.5
      const offsetX = -Math.sin(yaw) * dist
      const offsetZ = -Math.cos(yaw) * dist

      const desiredCamPos = new THREE.Vector3(
        dronePosition.x + offsetX,
        dronePosition.y + height,
        dronePosition.z + offsetZ,
      )

      camera.position.lerp(desiredCamPos, 0.08)

      // Look slightly ahead of the aircraft
      const lookAtTarget = new THREE.Vector3(
        dronePosition.x + Math.sin(yaw) * 3,
        dronePosition.y + 0.2,
        dronePosition.z + Math.cos(yaw) * 3,
      )
      camera.lookAt(lookAtTarget)
    } else if (cameraMode === 'fpv') {
      const yaw = droneRotation.y
      camera.position.set(
        dronePosition.x + Math.sin(yaw) * 0.45,
        dronePosition.y + 0.12,
        dronePosition.z + Math.cos(yaw) * 0.45,
      )
      const lookAtTarget = new THREE.Vector3(
        dronePosition.x + Math.sin(yaw) * 15,
        dronePosition.y,
        dronePosition.z + Math.cos(yaw) * 15,
      )
      camera.lookAt(lookAtTarget)
    }
  })

  if (cameraMode === 'orbit') {
    return (
      <OrbitControls
        enablePan
        enableZoom
        enableRotate
        minDistance={2}
        maxDistance={70}
        dampingFactor={0.05}
      />
    )
  }

  return null
}

// ─────────────────────────────────────────────────────────────
// Glass Cockpit HUD Overlay
// ─────────────────────────────────────────────────────────────

function ViewerToolbar({ cameraMode, setCameraMode, theme, setTheme, showAltProfile,
  setShowAltProfile, isLive, hasPath, expanded, setExpanded,
}: {
  cameraMode: CameraMode; setCameraMode: (mode: CameraMode) => void
  theme: EnvTheme; setTheme: (theme: EnvTheme) => void
  showAltProfile: boolean; setShowAltProfile: (show: boolean) => void
  isLive: boolean; hasPath: boolean; expanded: boolean; setExpanded: (value: boolean) => void
}) {
  return <div className={styles.toolbar}>
    <div className={styles.status}>
      <span className={`${styles.dot} ${isLive ? styles.live : hasPath ? styles.replay : ''}`} />
      {isLive ? 'Live flight' : hasPath ? 'Flight replay' : 'Awaiting telemetry'}
    </div>
    <div className={styles.controls}>
      <div className={styles.group} role="group" aria-label="Flight camera">
        {([{ mode: 'orbit', label: 'Orbit', icon: Orbit }, { mode: 'chase', label: 'Follow', icon: Video },
          { mode: 'fpv', label: 'Cockpit', icon: Eye }] as const).map(({ mode, label, icon: Icon }) =>
          <button key={mode} type="button" className={styles.button} aria-label={`${label} camera`}
            title={`${label} camera`} aria-pressed={cameraMode === mode} onClick={() => setCameraMode(mode)}>
            <Icon size={14} /><span className={styles.cameraLabel}>{label}</span>
          </button>)}
      </div>
      <div className={styles.group}>
        <button type="button" className={styles.button} onClick={() => setTheme(theme === 'day' ? 'night' : 'day')}
          aria-label={theme === 'day' ? 'Switch to night scene' : 'Switch to daylight scene'} title="Scene lighting">
          {theme === 'day' ? <Sun size={14} /> : <Moon size={14} />}
        </button>
        <button type="button" className={styles.button} disabled={!hasPath} aria-pressed={showAltProfile}
          aria-label="Altitude profile" title="Altitude profile" onClick={() => setShowAltProfile(!showAltProfile)}><BarChart2 size={14} /></button>
        <button type="button" className={styles.button} aria-label={expanded ? 'Collapse flight viewer' : 'Expand flight viewer'}
          title={expanded ? 'Collapse · Esc' : 'Expand viewer'} onClick={() => setExpanded(!expanded)}>
          {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
      </div>
    </div>
  </div>
}

function FlightReadouts({ info }: { info: WaypointInfo | null }) {
  const metrics = [
    { label: 'Altitude', value: info?.realAltitude_m, unit: 'm', icon: ArrowUp, digits: 1 },
    { label: 'Ground speed', value: info?.groundSpeed_ms, unit: 'm/s', icon: Gauge, digits: 1 },
    { label: 'Heading', value: info?.heading_deg, unit: '°', icon: Navigation, digits: 0 },
    { label: 'Battery', value: info?.batteryPct, unit: '%', icon: Battery, digits: 0 },
  ]
  return <dl className={styles.telemetry}>{metrics.map(({ label, value, unit, icon: Icon, digits }) =>
    <div key={label} className={styles.metric}><dt><Icon size={12} />{label}</dt>
      <dd>{value != null && Number.isFinite(value) ? value.toFixed(digits) : '—'}<small>{unit}</small></dd>
    </div>)}
  </dl>
}

// ─────────────────────────────────────────────────────────────
// Clean Altitude Profile Drawer
// ─────────────────────────────────────────────────────────────

function CleanAltitudeProfile({
  data,
  currentIndex,
  totalPoints,
  onClose,
}: {
  data: { distance: number; altitude: number }[]
  currentIndex: number
  totalPoints: number
  onClose: () => void
}) {
  // Downsample data so X-axis never has crowded numbers
  const sampledData = useMemo(() => {
    const step = Math.max(1, Math.floor(data.length / 40))
    return data.filter((_, i) => i % step === 0 || i === data.length - 1)
  }, [data])

  // Hooks must run unconditionally, so the early return comes after useMemo.
  if (data.length < 2) return null

  const currentDistance = data[Math.min(currentIndex, data.length - 1)]?.distance ?? 0

  return (
    <div className={styles.profile}>
      <div className="mb-1 flex items-center justify-between text-[10px] text-white/60">
        <span className="font-bold text-white">Altitude profile · m</span>
        <button
          onClick={onClose}
          aria-label="Close altitude profile"
          className="rounded px-1.5 py-0.5 hover:bg-white/10 text-white/70"
        >
          <X size={12} />
        </button>
      </div>

      <ResponsiveContainer width="100%" height={75}>
        <AreaChart data={sampledData} margin={{ top: 2, right: 10, bottom: 0, left: -25 }}>
          <defs>
            <linearGradient id="altGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#0284c7" stopOpacity={0.6} />
              <stop offset="95%" stopColor="#0284c7" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="distance"
            type="number"
            domain={['dataMin', 'dataMax']}
            tick={{ fontSize: 9, fill: '#94a3b8' }}
            tickLine={false}
            axisLine={{ stroke: '#334155' }}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fontSize: 9, fill: '#94a3b8' }}
            tickLine={false}
            axisLine={false}
            width={32}
          />
          <Tooltip
            contentStyle={{
              background: '#0f172a',
              border: '1px solid #334155',
              borderRadius: 6,
              fontSize: 10,
              color: '#38bdf8',
            }}
            formatter={(v: number) => [`${v} m`, 'Alt']}
            labelFormatter={(l: number) => `${l} m dist`}
          />
          <Area
            type="monotone"
            dataKey="altitude"
            stroke="#38bdf8"
            strokeWidth={1.8}
            fill="url(#altGrad)"
            dot={false}
            isAnimationActive={false}
          />
          <ReferenceLine x={currentDistance} stroke="#cbd5e1" strokeDasharray="3 3" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// Playback Control Bar
// ─────────────────────────────────────────────────────────────

function PlaybackBar({ fp, totalPoints, speedMultiplier, setSpeedMultiplier }: {
  fp: ReturnType<typeof useFlightPath>; totalPoints: number; speedMultiplier: number
  setSpeedMultiplier: (speed: number) => void
}) {
  const { currentIndex, isAnimating, progress, startAnimation, pauseAnimation,
    resetAnimation, seekTo, totalDistance, isLive } = fp
  const unavailable = totalPoints < 2 || isLive
  return <div className={styles.playback}>
    <div className={styles.transport}>
      <button type="button" className={`${styles.button} ${styles.play}`} disabled={unavailable}
        aria-label={isAnimating ? 'Pause replay' : 'Play replay'} onClick={isAnimating ? pauseAnimation : startAnimation}>
        {isAnimating ? <Pause size={15} /> : <Play size={15} />}
      </button>
      <button type="button" className={styles.button} disabled={unavailable} aria-label="Restart replay" onClick={resetAnimation}><RotateCcw size={14} /></button>
    </div>
    <div className={styles.scrubber}>
      <input type="range" aria-label="Flight replay position" disabled={unavailable} min={0}
        max={Math.max(0, totalPoints - 1)} value={Math.min(currentIndex, Math.max(0, totalPoints - 1))}
        onChange={(event) => { pauseAnimation(); seekTo(Number(event.target.value)) }} className={styles.timeline} />
      <div className={styles.timelineLabels}><span>{isLive ? 'Following live aircraft' : totalPoints ? `Sample ${currentIndex + 1} / ${totalPoints}` : 'No recording available'}</span>
        <span>{totalPoints ? `${Math.round(progress * 100)}%` : '—'}</span></div>
    </div>
    <div className={`${styles.group} ${styles.speedGroup}`} role="group" aria-label="Replay speed">
      {[1, 2, 4].map(speed => <button key={speed} type="button" className={styles.button} disabled={unavailable}
        aria-pressed={speedMultiplier === speed} aria-label={`${speed} times replay speed`} onClick={() => setSpeedMultiplier(speed)}>{speed}×</button>)}
    </div>
    <div className={styles.distance}>{totalPoints ? totalDistance.toLocaleString() : '—'} m<small>Recorded distance</small></div>
  </div>
}

// ─────────────────────────────────────────────────────────────
// Full 3D Flight Scene
// ─────────────────────────────────────────────────────────────

function FlightScene({
  flightPath,
  hoveredWaypoint,
  setHoveredWaypoint,
  cameraMode,
  theme,
}: {
  flightPath: ReturnType<typeof useFlightPath>
  hoveredWaypoint: WaypointInfo | null
  setHoveredWaypoint: (info: WaypointInfo | null) => void
  cameraMode: CameraMode
  theme: EnvTheme
}) {
  const { scaledPoints, dronePosition, droneRotation, waypointInfos, currentIndex } =
    flightPath

  const isDay = theme === 'day'

  const flownPoints = useMemo(
    () => scaledPoints.slice(0, currentIndex + 1),
    [scaledPoints, currentIndex],
  )

  return (
    <>
      {/* ── Environment & Atmosphere ─────────────────────────── */}
      {isDay ? (
        <>
          <Sky
            distance={450000}
            sunPosition={[80, 35, 80]}
            turbidity={8}
            rayleigh={4}
            mieCoefficient={0.005}
            mieDirectionalG={0.8}
          />
          <fog attach="fog" args={['#b8d8ec', 20, 100]} />
          <ambientLight intensity={0.65} color="#dbeafe" />
          <directionalLight
            position={[40, 60, 40]}
            intensity={1.5}
            castShadow
            shadow-mapSize={[1024, 1024]}
          />
        </>
      ) : (
        <>
          <color attach="background" args={['#0b121c']} />
          <fog attach="fog" args={['#050b14', 15, 80]} />
          <ambientLight intensity={0.8} color="#b5c9e0" />
          <directionalLight position={[20, 40, 20]} intensity={0.6} color="#93c5fd" />
        </>
      )}

      {/* ── Ground & Airfield ────────────────────────────────── */}
      <AirfieldGround theme={theme} />

      {/* ── Mission Flight Path ──────────────────────────────── */}
      {scaledPoints.length >= 2 && <Line points={scaledPoints} color="#587086" lineWidth={1} transparent opacity={0.35} />}
      <SmoothPath points={flownPoints} isDay={isDay} />
      <WaypointSpheres
        infos={waypointInfos}
        currentIndex={currentIndex}
        onHover={setHoveredWaypoint}
      />

      {/* ── Animated Drone Model ─────────────────────────────── */}
      {scaledPoints.length > 0 && <TravellingDrone position={dronePosition} rotation={droneRotation} theme={theme} />}

      {/* ── Dynamic Camera ──────────────────────────────────── */}
      <DynamicCameraController
        cameraMode={cameraMode}
        dronePosition={dronePosition}
        droneRotation={droneRotation}
      />
    </>
  )
}

// ─────────────────────────────────────────────────────────────
// Public Component
// ─────────────────────────────────────────────────────────────

export interface FlightPathViewerProps {
  animationSpeed?: number
  className?: string
  showAltProfile?: boolean
}

export default function FlightPathViewer({ animationSpeed = 1, className = '',
  showAltProfile: defaultShowAltProfile = false,
}: FlightPathViewerProps) {
  const [speedMultiplier, setSpeedMultiplier] = useState(1)
  const flightPath = useFlightPath(animationSpeed * speedMultiplier)
  const [cameraMode, setCameraMode] = useState<CameraMode>('orbit')
  const [theme, setTheme] = useState<EnvTheme>('night')
  const [showAltProfile, setShowAltProfile] = useState(defaultShowAltProfile)
  const [hoveredWP, setHoveredWP] = useState<WaypointInfo | null>(null)
  const [expanded, setExpanded] = useState(false)
  useEffect(() => {
    if (!expanded) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded])
  const { scaledPoints, altitudeProfile, waypointInfos, currentIndex, isLive } = flightPath
  const hasPath = scaledPoints.length >= 2
  const info = waypointInfos[currentIndex] ?? null
  return <div className={`${styles.frame} ${expanded ? styles.expanded : ''} ${className}`}>
    <ViewerToolbar {...{cameraMode, setCameraMode, theme, setTheme, showAltProfile, setShowAltProfile, isLive, hasPath, expanded, setExpanded}} />
    <div className={styles.stage}>
      <div className={styles.scene}>
        <Canvas camera={{ position: [10, 9, 10], fov: 48, near: 0.01, far: 300 }}
          gl={{ antialias: true }} shadows dpr={[1, 1.5]}>
          <Suspense fallback={null}>
            <FlightScene flightPath={flightPath} hoveredWaypoint={hoveredWP} setHoveredWaypoint={setHoveredWP} cameraMode={cameraMode} theme={theme} />
          </Suspense>
        </Canvas>
      </div>
      <div className={styles.legend}><span><i className="h-0.5 w-4 bg-cyan-300" />Flight trace</span><span><i className="h-0.5 w-4 bg-slate-500" />Recording</span></div>
      <div className={styles.compass}><Compass size={15} /><span>{info?.heading_deg != null ? `${Math.round(info.heading_deg).toString().padStart(3, '0')}°` : '—'}</span></div>
      {!hasPath && <div className={styles.empty}>
        <div className={styles.emptyIcon}><Radio size={25} strokeWidth={1.3} /></div>
        <h3>{isLive ? 'Waiting for the first samples' : 'Your mission, in motion'}</h3>
        <p>The aircraft’s flight trace will appear here when simulation telemetry arrives.</p>
      </div>}
      {hoveredWP && <div className={styles.sample}>Sample {hoveredWP.index + 1} · {hoveredWP.realAltitude_m.toFixed(1)} m altitude</div>}
      <FlightReadouts info={info} />
    </div>
    {hasPath && showAltProfile && <CleanAltitudeProfile data={altitudeProfile} currentIndex={currentIndex} totalPoints={scaledPoints.length} onClose={() => setShowAltProfile(false)} />}
    <PlaybackBar fp={flightPath} totalPoints={scaledPoints.length} speedMultiplier={speedMultiplier} setSpeedMultiplier={setSpeedMultiplier} />
    <div className={styles.footer}><span>{cameraMode === 'orbit' ? 'Drag to orbit · Right-drag to pan · Scroll to zoom' : cameraMode === 'chase' ? 'Camera follows the aircraft' : 'Aircraft forward view'}</span><span>{info?.flightMode ?? 'Telemetry-driven view'}</span></div>
  </div>
}
