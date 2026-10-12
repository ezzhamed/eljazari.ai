'use client'

import { useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import { Mesh } from 'three'

export default function DroneMesh() {
  const prop1 = useRef<Mesh>(null)
  const prop2 = useRef<Mesh>(null)
  const prop3 = useRef<Mesh>(null)
  const prop4 = useRef<Mesh>(null)

  // Animate propellers
  useFrame((_, delta) => {
    const speed = delta * 20
    ;[prop1, prop2, prop3, prop4].forEach((ref, i) => {
      if (ref.current) {
        ref.current.rotation.y += i % 2 === 0 ? speed : -speed
      }
    })
  })

  const armPositions: [number, number, number][] = [
    [0.35, 0, 0.35],
    [-0.35, 0, 0.35],
    [0.35, 0, -0.35],
    [-0.35, 0, -0.35],
  ]

  return (
    <group>
      {/* Body */}
      <mesh castShadow>
        <boxGeometry args={[0.3, 0.07, 0.3]} />
        <meshStandardMaterial color="#1a1a2e" metalness={0.8} roughness={0.2} />
      </mesh>

      {/* Arms + Motors + Propellers */}
      {armPositions.map((pos, i) => (
        <group key={i} position={pos}>
          {/* Arm */}
          <mesh>
            <cylinderGeometry args={[0.02, 0.02, 0.5, 8]} />
            <meshStandardMaterial color="#16213e" metalness={0.6} roughness={0.4} />
          </mesh>
          {/* Motor */}
          <mesh position={[0, 0.05, 0]}>
            <cylinderGeometry args={[0.04, 0.04, 0.05, 16]} />
            <meshStandardMaterial color="#0f3460" metalness={0.9} roughness={0.1} />
          </mesh>
          {/* Propeller */}
          <mesh
            ref={i === 0 ? prop1 : i === 1 ? prop2 : i === 2 ? prop3 : prop4}
            position={[0, 0.09, 0]}
          >
            <boxGeometry args={[0.3, 0.005, 0.04]} />
            <meshStandardMaterial
              color="#cdb892"
              transparent
              opacity={0.7}
              emissive="#cdb892"
              emissiveIntensity={0.2}
            />
          </mesh>
        </group>
      ))}
    </group>
  )
}
