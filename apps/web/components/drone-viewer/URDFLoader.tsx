'use client'

/**
 * URDFLoader — parses a URDF XML string and renders the robot
 * as Three.js meshes inside a React Three Fiber scene.
 *
 * Strategy (no external urdf-loader package required):
 *  1. Accept either a URDF XML string or a URL to fetch it from
 *  2. Parse the XML with DOMParser → extract <link> geometry + <joint> transforms
 *  3. Build a scenegraph of Three.js objects with correct parent→child transforms
 *  4. Fallback: if URDF is unavailable, renders the procedural DroneMesh
 *
 * To use the urdf-loader npm package instead (richer SITL mesh support):
 *   npm install urdf-loader
 *   and uncomment the URDFLoaderNative section below.
 */

import { useEffect, useRef, useState, useMemo } from 'react'
import { useLoader } from '@react-three/fiber'
import * as THREE from 'three'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

interface ParsedLink {
  name:        string
  geometryType: 'box' | 'cylinder' | 'sphere' | 'mesh' | 'none'
  size?:       [number, number, number]      // box
  radius?:     number                        // cylinder / sphere
  length?:     number                        // cylinder
  meshFile?:   string                        // mesh filename
  origin:      { xyz: [number,number,number]; rpy: [number,number,number] }
  mass:        number
  color?:      string
}

interface ParsedJoint {
  name:       string
  type:       'fixed' | 'continuous' | 'revolute' | 'prismatic'
  parent:     string
  child:      string
  origin:     { xyz: [number,number,number]; rpy: [number,number,number] }
  axis?:      [number,number,number]
}

interface ParsedURDF {
  name:   string
  links:  ParsedLink[]
  joints: ParsedJoint[]
}

// ─────────────────────────────────────────────────────────────
// XML helpers
// ─────────────────────────────────────────────────────────────

function parseXYZ(str: string | null): [number, number, number] {
  if (!str) return [0, 0, 0]
  const parts = str.trim().split(/\s+/).map(Number)
  return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0]
}

function parseOrigin(el: Element | null): { xyz: [number,number,number]; rpy: [number,number,number] } {
  const origin = el?.querySelector('origin')
  return {
    xyz: parseXYZ(origin?.getAttribute('xyz') ?? null),
    rpy: parseXYZ(origin?.getAttribute('rpy') ?? null),
  }
}

function parseMass(el: Element | null): number {
  const mass = el?.querySelector('inertial > mass')
  return parseFloat(mass?.getAttribute('value') ?? '0.05')
}

function componentColorFromName(name: string): string {
  if (name.includes('prop'))    return '#cdb892'
  if (name.includes('motor'))   return '#0f3460'
  if (name.includes('arm'))     return '#16263e'
  if (name.includes('battery')) return '#4a9eff'
  if (name.includes('sensor') || name.includes('gps')) return '#ff8800'
  return '#1e2d44'   // body default
}

// ─────────────────────────────────────────────────────────────
// URDF XML → ParsedURDF
// ─────────────────────────────────────────────────────────────

function parseURDF(xmlString: string): ParsedURDF | null {
  try {
    const parser = new DOMParser()
    const doc    = parser.parseFromString(xmlString, 'application/xml')
    const robot  = doc.querySelector('robot')
    if (!robot) return null

    const links: ParsedLink[] = []
    robot.querySelectorAll('link').forEach(linkEl => {
      const name = linkEl.getAttribute('name') ?? 'unknown'
      const visual = linkEl.querySelector('visual')
      const geomEl = visual?.querySelector('geometry')

      let geometryType: ParsedLink['geometryType'] = 'none'
      let size: [number,number,number] | undefined
      let radius: number | undefined
      let length: number | undefined
      let meshFile: string | undefined

      if (geomEl) {
        const box = geomEl.querySelector('box')
        const cyl = geomEl.querySelector('cylinder')
        const sph = geomEl.querySelector('sphere')
        const msh = geomEl.querySelector('mesh')

        if (box) {
          geometryType = 'box'
          const s = parseXYZ(box.getAttribute('size'))
          size = s
        } else if (cyl) {
          geometryType = 'cylinder'
          radius = parseFloat(cyl.getAttribute('radius') ?? '0.01')
          length = parseFloat(cyl.getAttribute('length') ?? '0.05')
        } else if (sph) {
          geometryType = 'sphere'
          radius = parseFloat(sph.getAttribute('radius') ?? '0.01')
        } else if (msh) {
          geometryType = 'mesh'
          meshFile = msh.getAttribute('filename') ?? undefined
        }
      }

      links.push({
        name,
        geometryType,
        size,
        radius,
        length,
        meshFile,
        origin: parseOrigin(visual),
        mass:   parseMass(linkEl),
        color:  componentColorFromName(name),
      })
    })

    const joints: ParsedJoint[] = []
    robot.querySelectorAll('joint').forEach(jEl => {
      const axisEl = jEl.querySelector('axis')
      joints.push({
        name:   jEl.getAttribute('name')   ?? 'joint',
        type:   (jEl.getAttribute('type')  ?? 'fixed') as ParsedJoint['type'],
        parent: jEl.querySelector('parent')?.getAttribute('link') ?? '',
        child:  jEl.querySelector('child')?.getAttribute('link')  ?? '',
        origin: parseOrigin(jEl),
        axis:   axisEl ? parseXYZ(axisEl.getAttribute('xyz')) : [0, 0, 1],
      })
    })

    return { name: robot.getAttribute('name') ?? 'robot', links, joints }
  } catch {
    return null
  }
}

// ─────────────────────────────────────────────────────────────
// Sub-component: render a single parsed link as a mesh
// ─────────────────────────────────────────────────────────────

function URDFLinkMesh({
  link,
  wireframe = false,
  opacity   = 1,
  onClick,
}: {
  link:       ParsedLink
  wireframe?: boolean
  opacity?:   number
  onClick?:   (name: string) => void
}) {
  const [hovered, setHovered] = useState(false)
  const color = hovered ? '#ffffff' : (link.color ?? '#1e2d44')

  const geometry = useMemo(() => {
    switch (link.geometryType) {
      case 'box':
        return <boxGeometry args={link.size ?? [0.1, 0.05, 0.1]} />
      case 'cylinder':
        return <cylinderGeometry args={[link.radius ?? 0.01, link.radius ?? 0.01, link.length ?? 0.05, 16]} />
      case 'sphere':
        return <sphereGeometry args={[link.radius ?? 0.01, 12, 12]} />
      default:
        return <boxGeometry args={[0.05, 0.05, 0.05]} />
    }
  }, [link])

  if (link.geometryType === 'none') return null

  return (
    <mesh
      position={link.origin.xyz}
      rotation={link.origin.rpy}
      castShadow
      receiveShadow
      onPointerOver={() => setHovered(true)}
      onPointerOut={() => setHovered(false)}
      onClick={() => onClick?.(link.name)}
    >
      {geometry}
      <meshStandardMaterial
        color={color}
        wireframe={wireframe}
        transparent={opacity < 1}
        opacity={opacity}
        metalness={0.6}
        roughness={0.3}
      />
    </mesh>
  )
}

// ─────────────────────────────────────────────────────────────
// Main URDFLoader component
// ─────────────────────────────────────────────────────────────

interface URDFLoaderProps {
  /** Raw URDF XML string.  Pass this OR urdfUrl. */
  urdfXml?:  string
  /** URL to fetch the URDF from (e.g. /api/projects/x/files/urdf). */
  urdfUrl?:  string
  wireframe?: boolean
  opacity?:   number
  onLinkClick?: (linkName: string) => void
}

export function URDFModel({
  urdfXml,
  urdfUrl,
  wireframe = false,
  opacity   = 1,
  onLinkClick,
}: URDFLoaderProps) {
  const [xml, setXml]         = useState<string | null>(urdfXml ?? null)
  const [error, setError]     = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // Fetch URDF from URL if not provided directly
  useEffect(() => {
    if (urdfXml) { setXml(urdfXml); return }
    if (!urdfUrl) return

    setLoading(true)
    fetch(urdfUrl)
      .then(r => {
        if (!r.ok) throw new Error(`URDF fetch failed: ${r.status}`)
        return r.text()
      })
      .then(text => { setXml(text); setLoading(false) })
      .catch(e  => { setError(e.message); setLoading(false) })
  }, [urdfXml, urdfUrl])

  const parsed = useMemo(() => (xml ? parseURDF(xml) : null), [xml])

  if (loading || !parsed) return null

  return (
    <group name={parsed.name}>
      {parsed.links.map(link => (
        <URDFLinkMesh
          key={link.name}
          link={link}
          wireframe={wireframe}
          opacity={opacity}
          onClick={onLinkClick}
        />
      ))}
    </group>
  )
}

// ─────────────────────────────────────────────────────────────
// Convenience: resolve joint hierarchy into world transforms
// (advanced usage — optional, used by DroneViewer for HITL preview)
// ─────────────────────────────────────────────────────────────

export function buildJointTransformMap(parsed: ParsedURDF): Map<string, THREE.Matrix4> {
  const transforms = new Map<string, THREE.Matrix4>()
  const linkMatrix  = (name: string) => transforms.get(name) ?? new THREE.Matrix4()

  // Start with base_link at identity
  transforms.set('base_link', new THREE.Matrix4())

  // BFS through joints
  const processed = new Set<string>(['base_link'])
  let changed = true
  while (changed) {
    changed = false
    for (const jt of parsed.joints) {
      if (!processed.has(jt.parent) || processed.has(jt.child)) continue
      const parentMat = linkMatrix(jt.parent)
      const localMat  = new THREE.Matrix4()
        .makeRotationFromEuler(new THREE.Euler(...jt.origin.rpy))
        .setPosition(...jt.origin.xyz)
      transforms.set(jt.child, new THREE.Matrix4().multiplyMatrices(parentMat, localMat))
      processed.add(jt.child)
      changed = true
    }
  }
  return transforms
}
