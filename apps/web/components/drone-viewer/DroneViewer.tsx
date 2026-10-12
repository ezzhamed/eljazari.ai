'use client'

/**
 * DroneViewer — full-featured interactive 3D drone design viewer.
 *
 * Features:
 *  - Parametric drone mesh driven by DroneDesignTree from the store
 *  - URDF model overlay when a URDF file is available
 *  - View modes: solid / wireframe / transparent
 *  - Animated propellers with configurable speed
 *  - Click-to-inspect component annotations (drei Html)
 *  - World-axes helper + ground grid
 *  - Camera preset buttons (isometric / top / front / side)
 *  - Screenshot button (PNG download)
 *  - Gizmo viewport (compass in corner)
 *  - Responsive: fills its container
 */

import { useRef, useEffect, Suspense, useCallback, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import {
  OrbitControls,
  Grid,
  Html,
  GizmoHelper,
  GizmoViewport,
  Environment,
} from '@react-three/drei'
import * as THREE from 'three'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useDroneModel } from '@/lib/hooks/useDroneModel'
import { URDFModel } from './URDFLoader'
import {
  ViewMode,
  CameraPreset,
  COMPONENT_COLORS,
  CAMERA_PRESETS,
  DEFAULT_ANNOTATIONS,
  AnnotationDef,
} from './controls'
import { resolveDesign } from './resolve-design'
import { useDesignParamsStore } from '@/lib/stores/design-params-store'

// ─────────────────────────────────────────────────────────────
// Spinning propeller — works for any rotor count and spin axis
// ─────────────────────────────────────────────────────────────

function SpinningProp({
  length, spin, animate, speed, wireframe, axis = 'y', onClick,
}: {
  length: number
  spin: number
  animate: boolean
  speed: number
  wireframe: boolean
  axis?: 'y' | 'z'
  onClick?: (e: { stopPropagation: () => void }) => void
}) {
  const ref = useRef<THREE.Mesh>(null)
  useFrame((_, dt) => {
    if (!animate || !ref.current) return
    const d = dt * speed * spin
    if (axis === 'z') ref.current.rotation.z += d
    else ref.current.rotation.y += d
  })
  const args: [number, number, number] =
    axis === 'z' ? [length, 0.03, 0.004] : [length, 0.004, 0.035]
  return (
    <mesh ref={ref} onClick={onClick}>
      <boxGeometry args={args} />
      <meshStandardMaterial
        color={wireframe ? COMPONENT_COLORS.propeller : '#cdb892'}
        wireframe={wireframe}
        transparent
        opacity={wireframe ? 1 : 0.75}
        emissive="#cdb892"
        emissiveIntensity={wireframe ? 0 : 0.25}
        side={THREE.DoubleSide}
      />
    </mesh>
  )
}

// ─────────────────────────────────────────────────────────────
// Parametric drone mesh
// ─────────────────────────────────────────────────────────────

interface DroneMeshProps {
  viewMode:        ViewMode
  propSpeed:       number
  animateProps:    boolean
  selectedComponent: string | null
  onSelect:        (id: string | null) => void
  /** Override the arm-length-derived scale — used by the flight-path follower
   *  so the same generated model renders at a consistent, readable size. */
  armScaleOverride?: number
}

export function ParametricDroneMesh({
  viewMode,
  propSpeed,
  animateProps,
  selectedComponent,
  onSelect,
  armScaleOverride,
}: DroneMeshProps) {
  const { design } = useDroneStore()
  const params = useDesignParamsStore(s => s.params)
  const tree = design?.design_tree

  // The agent's design is the baseline; the parameter panel overrides individual
  // values on top of it. See components/drone-viewer/resolve-design.ts.
  const resolved = resolveDesign(tree, params)
  const layout = { kind: resolved.kind, rotorCount: resolved.rotorCount, label: resolved.label }

  const armLength = resolved.armLengthM
  const armScale  = armScaleOverride ?? resolved.armScale
  const batteryScale = resolved.batteryScale
  // SpinningProp sizes its box by full span, so this is the diameter, not a radius.
  const propSpan = resolved.propDiameterM

  const opacity     = viewMode === 'transparent' ? 0.35 : 1
  const wireframe   = viewMode === 'wireframe'
  const transparent = viewMode === 'transparent'

  function matProps(component: keyof typeof COMPONENT_COLORS) {
    const isSelected = selectedComponent === component
    return {
      color:           isSelected ? '#ffffff' : COMPONENT_COLORS[component],
      wireframe,
      transparent,
      opacity,
      metalness:       0.7,
      roughness:       0.25,
      emissive:        isSelected ? COMPONENT_COLORS[component] : '#000000',
      emissiveIntensity: isSelected ? 0.4 : 0,
    }
  }

  // ── Fixed-wing (and VTOL hybrid) airframe ────────────────────
  if (layout.kind === 'fixed_wing' || layout.kind === 'hybrid') {
    const s = armScale
    const isHybrid = layout.kind === 'hybrid'
    const liftPos: [number, number, number][] = [
      [ 0.55 * s, 0,  0.3 * s], [-0.55 * s, 0,  0.3 * s],
      [ 0.55 * s, 0, -0.3 * s], [-0.55 * s, 0, -0.3 * s],
    ]
    return (
      <group>
        {/* Fuselage */}
        <mesh castShadow onClick={e => { e.stopPropagation(); onSelect('body') }}>
          <boxGeometry args={[0.08 * s, 0.08 * s, 0.95 * s]} />
          <meshStandardMaterial {...matProps('body')} />
        </mesh>
        {/* Nose cone */}
        <mesh position={[0, 0, 0.52 * s]} rotation={[Math.PI / 2, 0, 0]}>
          <coneGeometry args={[0.05 * s, 0.14 * s, 16]} />
          <meshStandardMaterial {...matProps('body')} />
        </mesh>
        {/* Main wing (the two wings) */}
        <mesh position={[0, 0.012 * s, 0.06 * s]} castShadow onClick={e => { e.stopPropagation(); onSelect('arm') }}>
          <boxGeometry args={[1.7 * s, 0.02 * s, 0.26 * s]} />
          <meshStandardMaterial {...matProps('arm')} />
        </mesh>
        {/* Horizontal stabiliser */}
        <mesh position={[0, 0.012 * s, -0.42 * s]} onClick={e => { e.stopPropagation(); onSelect('arm') }}>
          <boxGeometry args={[0.62 * s, 0.02 * s, 0.14 * s]} />
          <meshStandardMaterial {...matProps('arm')} />
        </mesh>
        {/* Vertical fin */}
        <mesh position={[0, 0.1 * s, -0.44 * s]}>
          <boxGeometry args={[0.02 * s, 0.18 * s, 0.14 * s]} />
          <meshStandardMaterial {...matProps('arm')} />
        </mesh>
        {/* Battery in fuselage */}
        <mesh position={[0, -0.03 * s, 0.1 * s]} onClick={e => { e.stopPropagation(); onSelect('battery') }}>
          <boxGeometry args={[0.06 * s, 0.03 * s, 0.32 * s]} />
          <meshStandardMaterial {...matProps('battery')} />
        </mesh>
        {/* Nose tractor propeller (spins about forward axis) */}
        <group position={[0, 0, 0.62 * s]}>
          <SpinningProp length={0.5 * s} spin={1} animate={animateProps} speed={propSpeed} wireframe={wireframe} axis="z" onClick={e => { e.stopPropagation(); onSelect('prop_0') }} />
        </group>
        {/* VTOL lift rotors (hybrid only) */}
        {isHybrid && liftPos.map((pos, i) => (
          <group key={i} position={pos}>
            <mesh position={[0, 0.02 * s, 0]}>
              <cylinderGeometry args={[0.02, 0.02, 0.03, 12]} />
              <meshStandardMaterial {...matProps('motor')} />
            </mesh>
            <group position={[0, 0.05 * s, 0]}>
              <SpinningProp length={0.34 * s} spin={i % 2 === 0 ? 1 : -1} animate={animateProps} speed={propSpeed} wireframe={wireframe} onClick={e => { e.stopPropagation(); onSelect(`motor_${i + 1}`) }} />
            </group>
          </group>
        ))}
      </group>
    )
  }

  // ── Multirotor: N rotors arranged radially ───────────────────
  const n = layout.rotorCount
  const ring = 0.36 * armScale
  return (
    <group>
      {/* Central body */}
      <mesh castShadow onClick={e => { e.stopPropagation(); onSelect('body') }}>
        <boxGeometry args={[0.26 * armScale, 0.06, 0.26 * armScale]} />
        <meshStandardMaterial {...matProps('body')} />
      </mesh>

      {/* Battery plate — grows with pack capacity */}
      <mesh position={[0, -0.045, 0]} castShadow onClick={e => { e.stopPropagation(); onSelect('battery') }}>
        <boxGeometry args={[0.18 * armScale * batteryScale, 0.03 * batteryScale, 0.12 * armScale * batteryScale]} />
        <meshStandardMaterial {...matProps('battery')} />
      </mesh>

      {/* GPS dome */}
      <mesh position={[0, 0.055, 0]} onClick={e => { e.stopPropagation(); onSelect('sensor') }}>
        <sphereGeometry args={[0.025, 12, 12]} />
        <meshStandardMaterial {...matProps('sensor')} />
      </mesh>

      {/* Arms + motors + propellers (radial) */}
      {Array.from({ length: n }, (_, i) => {
        const a = (Math.PI * 2 * i) / n - Math.PI / 2
        const x = Math.cos(a) * ring
        const z = Math.sin(a) * ring
        return (
          <group key={i}>
            {/* Arm from centre outward */}
            <mesh position={[x / 2, 0, z / 2]} rotation={[0, -a, 0]} castShadow onClick={e => { e.stopPropagation(); onSelect('arm') }}>
              <boxGeometry args={[ring, 0.018, 0.03]} />
              <meshStandardMaterial {...matProps('arm')} />
            </mesh>
            {/* Motor + propeller at rotor position */}
            <group position={[x, 0, z]}>
              <mesh position={[0, 0.04, 0]} castShadow onClick={e => { e.stopPropagation(); onSelect(`motor_${i}`) }}>
                <cylinderGeometry args={[0.022, 0.022, 0.028, 16]} />
                <meshStandardMaterial {...matProps('motor')} />
              </mesh>
              <group position={[0, 0.06, 0]}>
                <SpinningProp length={propSpan} spin={i % 2 === 0 ? 1 : -1} animate={animateProps} speed={propSpeed} wireframe={wireframe} onClick={e => { e.stopPropagation(); onSelect(`prop_${i}`) }} />
              </group>
            </group>
          </group>
        )
      })}
    </group>
  )
}

// ─────────────────────────────────────────────────────────────
// Component annotation label (inside Canvas via Html)
// ─────────────────────────────────────────────────────────────

function AnnotationLabel({
  ann,
  selected,
  onClick,
}: {
  ann:      AnnotationDef
  selected: boolean
  onClick:  () => void
}) {
  return (
    <Html
      position={ann.position}
      center
      distanceFactor={3}
      zIndexRange={[10, 100]}
      occlude={false}
    >
      <button
        onClick={onClick}
        className={`
          px-2 py-1 rounded text-xs font-mono whitespace-nowrap cursor-pointer
          border transition-all duration-150 select-none
          ${selected
            ? 'bg-brand-500/90 border-brand-400 text-black font-bold shadow-lg shadow-brand-500/30'
            : 'bg-[#0a0a0a]/80 border-brand-800/50 text-brand-400 hover:border-brand-500'}
        `}
      >
        {ann.label}
        {selected && (
          <div className="mt-1 text-[10px] text-black/80 font-sans max-w-[140px] whitespace-normal">
            {ann.description}
          </div>
        )}
      </button>
    </Html>
  )
}

// ─────────────────────────────────────────────────────────────
// Screenshot helper (lives inside Canvas to access useThree)
// ─────────────────────────────────────────────────────────────

function ScreenshotRegister({ onReady }: { onReady: (fn: () => void) => void }) {
  const { gl, scene, camera } = useThree()
  useEffect(() => {
    onReady(() => {
      gl.render(scene, camera)
      const link = document.createElement('a')
      link.download = `drone-view-${Date.now()}.png`
      link.href = gl.domElement.toDataURL('image/png')
      link.click()
    })
  }, [gl, scene, camera, onReady])
  return null
}

// ─────────────────────────────────────────────────────────────
// Camera reset helper
// ─────────────────────────────────────────────────────────────

function CameraReset({
  preset,
  triggerKey,
}: {
  preset:     CameraPreset
  triggerKey: number
}) {
  const { camera } = useThree()
  useEffect(() => {
    const [x, y, z] = CAMERA_PRESETS[preset]
    camera.position.set(x, y, z)
    camera.lookAt(0, 0, 0)
  }, [preset, triggerKey, camera])
  return null
}

// ─────────────────────────────────────────────────────────────
// 3D Scene (Canvas child)
// ─────────────────────────────────────────────────────────────

interface SceneProps {
  controls:         ReturnType<typeof useDroneModel>
  urdfUrl?:         string
  cameraResetKey:   number
}

function DroneScene({ controls, urdfUrl, cameraResetKey }: SceneProps) {
  const {
    viewMode, propSpeed, animateProps,
    showAnnotations, showAxes, showGrid,
    selectedComponent, setSelectedComponent,
    cameraPreset, registerScreenshot,
  } = controls

  const { design } = useDroneStore()
  const hasURDF = !!urdfUrl

  return (
    <>
      {/* ── Screenshot + camera helpers ───────────────────── */}
      <ScreenshotRegister onReady={registerScreenshot} />
      <CameraReset preset={cameraPreset} triggerKey={cameraResetKey} />

      {/* ── Lighting ──────────────────────────────────────── */}
      <ambientLight intensity={0.4} />
      <directionalLight
        position={[5, 10, 5]}
        intensity={1.2}
        castShadow
        shadow-mapSize={[2048, 2048]}
      />
      <pointLight position={[-3, 4, -3]} intensity={0.5} color="#6699ff" />
      <pointLight position={[0, 2, 0]}   intensity={0.3} color="#cdb892" />
      <Environment preset="night" />

      {/* ── Drone model ───────────────────────────────────── */}
      {hasURDF ? (
        <URDFModel
          urdfUrl={urdfUrl}
          wireframe={viewMode === 'wireframe'}
          opacity={viewMode === 'transparent' ? 0.35 : 1}
          onLinkClick={setSelectedComponent}
        />
      ) : (
        <ParametricDroneMesh
          viewMode={viewMode}
          propSpeed={propSpeed}
          animateProps={animateProps}
          selectedComponent={selectedComponent}
          onSelect={setSelectedComponent}
        />
      )}

      {/* ── Annotations ───────────────────────────────────── */}
      {showAnnotations &&
        DEFAULT_ANNOTATIONS.map(ann => (
          <AnnotationLabel
            key={ann.id}
            ann={ann}
            selected={selectedComponent === ann.id}
            onClick={() =>
              setSelectedComponent(selectedComponent === ann.id ? null : ann.id)
            }
          />
        ))}

      {/* ── World-axes helper ─────────────────────────────── */}
      {showAxes && <axesHelper args={[1]} />}

      {/* ── Ground grid ───────────────────────────────────── */}
      {showGrid && (
        <Grid
          args={[10, 10]}
          cellSize={0.5}
          cellThickness={0.5}
          cellColor="#1a1a1a"
          sectionSize={2.5}
          sectionColor="#242424"
          fadeDistance={20}
          position={[0, -0.55, 0]}
        />
      )}

      {/* ── Shadow plane ──────────────────────────────────── */}
      <mesh
        rotation={[-Math.PI / 2, 0, 0]}
        position={[0, -0.55, 0]}
        receiveShadow
      >
        <planeGeometry args={[20, 20]} />
        <shadowMaterial transparent opacity={0.25} />
      </mesh>

      {/* ── Orbit controls ────────────────────────────────── */}
      <OrbitControls
        enablePan
        enableZoom
        enableRotate
        minDistance={0.5}
        maxDistance={20}
        target={[0, 0, 0]}
      />

      {/* ── Corner gizmo ──────────────────────────────────── */}
      <GizmoHelper alignment="bottom-right" margin={[60, 60]}>
        <GizmoViewport
          axisColors={['#ff4444', '#44ff44', '#4488ff']}
          labelColor="white"
        />
      </GizmoHelper>
    </>
  )
}

// ─────────────────────────────────────────────────────────────
// Toolbar button helpers
// ─────────────────────────────────────────────────────────────

function ToolBtn({
  active,
  onClick,
  title,
  children,
}: {
  active?:  boolean
  onClick:  () => void
  title:    string
  children: React.ReactNode
}) {
  return (
    <button
      title={title}
      onClick={onClick}
      className={`
        px-2.5 py-1.5 rounded text-xs font-mono transition-all duration-150
        ${active
          ? 'bg-brand-500/20 border border-brand-500/60 text-brand-400'
          : 'bg-[#0d0d0d]/60 border border-[#242424]/40 text-gray-400 hover:text-brand-400 hover:border-brand-800'}
      `}
    >
      {children}
    </button>
  )
}

// ─────────────────────────────────────────────────────────────
// Public component
// ─────────────────────────────────────────────────────────────

interface DroneViewerProps {
  /** If provided, loads and renders the URDF model from this URL */
  urdfUrl?: string
  className?: string
}

export default function DroneViewer({ urdfUrl, className = '' }: DroneViewerProps) {
  const controls         = useDroneModel()
  const [resetKey, setResetKey] = useState(0)

  const {
    viewMode, setViewMode,
    showAnnotations, toggleAnnotations,
    showAxes, toggleAxes,
    showGrid, toggleGrid,
    animateProps, toggleAnimateProps,
    cameraPreset, setCameraPreset,
    takeScreenshot,
  } = controls

  const viewModes: ViewMode[] = ['solid', 'wireframe', 'transparent']
  const cameraPresets: CameraPreset[] = ['isometric', 'top', 'front', 'side']

  return (
    <div className={`relative w-full h-full flex flex-col bg-[#0a0a0a] rounded-xl overflow-hidden ${className}`}>

      {/* ── Toolbar ─────────────────────────────────────── */}
      <div className="absolute top-2 left-2 z-10 flex flex-wrap gap-1.5">

        {/* View mode */}
        <div className="flex gap-1 bg-[#0d0d0d]/80 rounded border border-[#242424]/30 p-0.5 backdrop-blur-sm">
          {viewModes.map(m => (
            <ToolBtn key={m} active={viewMode === m} onClick={() => setViewMode(m)} title={`${m} mode`}>
              {m === 'solid' ? '●' : m === 'wireframe' ? '⬡' : '◎'} {m}
            </ToolBtn>
          ))}
        </div>

        {/* Toggles */}
        <div className="flex gap-1 bg-[#0d0d0d]/80 rounded border border-[#242424]/30 p-0.5 backdrop-blur-sm">
          <ToolBtn active={showAnnotations} onClick={toggleAnnotations} title="Annotations">⊕ labels</ToolBtn>
          <ToolBtn active={showAxes}        onClick={toggleAxes}        title="Axes">⊞ axes</ToolBtn>
          <ToolBtn active={showGrid}        onClick={toggleGrid}        title="Grid">⊟ grid</ToolBtn>
          <ToolBtn active={animateProps}    onClick={toggleAnimateProps} title="Propellers">⟳ props</ToolBtn>
        </div>

        {/* Camera presets */}
        <div className="flex gap-1 bg-[#0d0d0d]/80 rounded border border-[#242424]/30 p-0.5 backdrop-blur-sm">
          {cameraPresets.map(p => (
            <ToolBtn key={p} active={cameraPreset === p} onClick={() => { setCameraPreset(p); setResetKey(k => k + 1) }} title={`${p} view`}>
              {p === 'isometric' ? '⬡' : p === 'top' ? '↓' : p === 'front' ? '→' : '↗'} {p}
            </ToolBtn>
          ))}
        </div>

        {/* Screenshot */}
        <ToolBtn onClick={takeScreenshot} title="Save PNG">
          Save PNG
        </ToolBtn>
      </div>

      {/* ── Selected component info ─────────────────────── */}
      {controls.selectedComponent && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 bg-[#0d0d0d]/90 border border-brand-800/40 rounded-lg px-4 py-2 text-xs font-mono text-brand-400 backdrop-blur-sm">
          <span className="text-gray-500">Selected: </span>
          {controls.selectedComponent}
          <button
            onClick={() => controls.setSelectedComponent(null)}
            className="ml-3 text-gray-600 hover:text-red-400"
          >
            ✕
          </button>
        </div>
      )}

      {/* ── 3D Canvas ───────────────────────────────────── */}
      <Canvas
        camera={{ position: CAMERA_PRESETS.isometric, fov: 50, near: 0.01, far: 100 }}
        gl={{ antialias: true, preserveDrawingBuffer: true }}
        shadows
        dpr={[1, 2]}
        style={{ width: '100%', height: '100%' }}
        onPointerMissed={() => controls.setSelectedComponent(null)}
      >
        <Suspense fallback={null}>
          <DroneScene
            controls={controls}
            urdfUrl={urdfUrl}
            cameraResetKey={resetKey}
          />
        </Suspense>
      </Canvas>

      {/* ── Coordinate readout (bottom-right) ───────────── */}
      <div className="absolute bottom-2 right-2 text-[10px] font-mono text-gray-600 pointer-events-none">
        Three.js / R3F — eljazari.ai viewer
      </div>
    </div>
  )
}
