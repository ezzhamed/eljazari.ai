'use client'

/**
 * LeafletMap — Phase 4.2 WebGCS interactive map
 *
 * Features:
 *  - ESRI satellite tile layer (free, no API key)
 *  - Real-time drone marker with heading arrow
 *  - Planned path (dashed cyan polyline)
 *  - Actual flight path (solid green polyline)
 *  - Geofence circle (red dashed)
 *  - Waypoint drawing mode (click-to-add, right-click to remove)
 *  - Home marker, scale bar, layer toggle
 *
 * NOTE: Must be used inside dynamic(() => ..., { ssr: false }) wrapper
 * because Leaflet depends on `window`.
 */

import { useEffect, useRef, useState, useCallback, useMemo, type CSSProperties } from 'react'
// Leaflet's own layout rules: without them the tiles are not absolutely
// positioned and Tailwind's `img { max-width: 100% }` shrinks them, which left
// the map as a few scattered tiles.
import 'leaflet/dist/leaflet.css'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useWSStore } from '@/lib/stores/ws-store'

// Inline Leaflet CSS to avoid SSR issues
const LEAFLET_CSS = `
.leaflet-container { background: #0e0e0e; font-family: monospace; }
.leaflet-control-attribution { display: none; }
.leaflet-control-zoom a { background: #121212 !important; color: #cdb892 !important; border-color: #b89b6e40 !important; }
.leaflet-control-zoom a:hover { background: #1f1f1f !important; }
.drone-marker-icon { background: transparent; border: none; }
.wp-marker { background: transparent; border: none; }
`

interface WaypointDraw {
  lat: number
  lon: number
  alt_m: number
  hover_s: number
}

interface LeafletMapProps {
  className?: string
  style?: CSSProperties
  /** Metres; 0 draws none. Only a caller that knows the mission's geofence should pass one. */
  geofenceRadius?: number
  /** Where the map opens before any telemetry. Home then comes from the first sample. */
  homeLat?: number
  homeLon?: number
}

// NED → GPS conversion (approximate, good for < 5 km range)
function nedToLatLon(north_m: number, east_m: number, homeLat: number, homeLon: number) {
  const lat = homeLat + north_m / 111111
  const lon = homeLon + east_m / (111111 * Math.cos((homeLat * Math.PI) / 180))
  return [lat, lon] as [number, number]
}

export default function LeafletMap({
  className = '',
  geofenceRadius = 0,
  homeLat = 24.7136,
  homeLon = 46.6753,
}: LeafletMapProps) {
  const mapRef         = useRef<any>(null)
  const mapContainerRef = useRef<HTMLDivElement>(null)
  const droneMarkerRef = useRef<any>(null)
  const pathLayerRef   = useRef<any>(null)
  const plannedLayerRef = useRef<any>(null)
  const geofenceRef    = useRef<any>(null)
  const wpLayerRef     = useRef<any>(null)
  const cssInjectedRef = useRef(false)
  const homeMarkerRef  = useRef<any>(null)

  const { telemetryStream } = useDroneStore()
  const { socket } = useWSStore()

  const [drawMode, setDrawMode]   = useState(false)
  const [drawnWps, setDrawnWps]   = useState<WaypointDraw[]>([])
  const [mavlinkTelemetry, setMavlinkTelemetry] = useState<any>(null)
  const [showSatellite, setShowSatellite] = useState(true)
  const [mapReady, setMapReady] = useState(false)

  // Listen for MAVLink telemetry from backend
  useEffect(() => {
    if (!socket) return
    const handler = (data: any) => {
      if (data.type === 'full' || data.type === 'position') {
        setMavlinkTelemetry((prev: any) => ({ ...prev, ...data }))
      }
    }
    socket.on('mavlink_telemetry', handler)
    return () => { socket.off('mavlink_telemetry', handler) }
  }, [socket])

  // Inject CSS once
  useEffect(() => {
    if (cssInjectedRef.current) return
    cssInjectedRef.current = true
    const style = document.createElement('style')
    style.textContent = LEAFLET_CSS
    document.head.appendChild(style)
  }, [])

  // Init Leaflet map
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return
    // React runs this effect twice in development; the second run must not
    // initialise the same container while the first is still importing Leaflet.
    let cancelled = false

    const init = async () => {
      const L = (await import('leaflet')).default
      if (cancelled || !mapContainerRef.current || mapRef.current) return
      // Fix default icon path issue in Next.js
      delete (L.Icon.Default.prototype as any)._getIconUrl
      L.Icon.Default.mergeOptions({
        iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png',
        iconUrl:       'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
        shadowUrl:     'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
      })

      const map = L.map(mapContainerRef.current!, {
        center: [homeLat, homeLon],
        zoom: 17,
        zoomControl: true,
        attributionControl: false,
      })

      // Satellite tile layer (ESRI - free, no API key)
      const satelliteLayer = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        { maxZoom: 20, opacity: 0.85 }
      )
      // Dark map fallback
      const darkLayer = L.tileLayer(
        'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        { maxZoom: 20, opacity: 1 }
      )

      if (showSatellite) {
        satelliteLayer.addTo(map)
      } else {
        darkLayer.addTo(map)
      }

      ;(map as any)._satelliteLayer = satelliteLayer
      ;(map as any)._darkLayer = darkLayer

      // Home marker
      const homeIcon = L.divIcon({
        className: 'drone-marker-icon',
        html: `<div style="
          width:24px;height:24px;border-radius:50%;
          background:#cdb89222;border:2px solid #cdb892;
          display:flex;align-items:center;justify-content:center;
          font-size:10px;color:#cdb892;font-family:monospace;font-weight:bold;
        ">H</div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      })
      homeMarkerRef.current = L.marker([homeLat, homeLon], { icon: homeIcon, zIndexOffset: 100 })
        .bindTooltip('HOME', { permanent: false, className: 'leaflet-tooltip' })
        .addTo(map)

      // Geofence circle
      if (geofenceRadius > 0) {
        geofenceRef.current = L.circle([homeLat, homeLon], {
          radius: geofenceRadius,
          color: '#d65252',
          weight: 1.5,
          dashArray: '6 4',
          fillOpacity: 0.04,
          fillColor: '#d65252',
        }).addTo(map)
      }

      // Flight path layers
      pathLayerRef.current = L.polyline([], {
        color: '#cdb892',
        weight: 2,
        opacity: 0.7,
      }).addTo(map)

      plannedLayerRef.current = L.polyline([], {
        color: '#71a0d6',
        weight: 2,
        opacity: 0.6,
        dashArray: '8 4',
      }).addTo(map)

      // Waypoint group layer
      wpLayerRef.current = L.layerGroup().addTo(map)

      // Drone marker (SVG arrow)
      const droneIcon = L.divIcon({
        className: 'drone-marker-icon',
        html: `<div id="drone-arrow" style="
          width:0;height:0;position:relative;
          filter:drop-shadow(0 0 3px rgba(0,0,0,0.9));
        ">
          <svg width="28" height="28" viewBox="0 0 28 28" style="position:absolute;top:-14px;left:-14px;">
            <polygon points="14,2 20,22 14,18 8,22"
              fill="#ffffff" stroke="#0e0e0e" stroke-width="1.5"/>
          </svg>
        </div>`,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      })
      droneMarkerRef.current = L.marker([homeLat, homeLon], {
        icon: droneIcon,
        zIndexOffset: 1000,
      }).addTo(map)

      // Click-to-add waypoints
      map.on('click', (e: any) => {
        if (!(map as any)._drawMode) return
        const wp: WaypointDraw = { lat: e.latlng.lat, lon: e.latlng.lng, alt_m: 20, hover_s: 0 }
        setDrawnWps(prev => {
          const next = [...prev, wp]
          refreshWpMarkers(L, map, next)
          return next
        })
      })

      mapRef.current = map
      setMapReady(true)
      // The grid may still be settling; measure again once it has.
      window.setTimeout(() => map.invalidateSize(), 0)
    }

    init()
    return () => {
      cancelled = true
      mapRef.current?.remove()
      mapRef.current = null
      setMapReady(false)
    }
  }, [homeLat, homeLon, geofenceRadius]) // eslint-disable-line react-hooks/exhaustive-deps

  // Helper: redraw waypoint markers
  const refreshWpMarkers = useCallback((L: any, map: any, wps: WaypointDraw[]) => {
    if (!wpLayerRef.current) return
    wpLayerRef.current.clearLayers()
    wps.forEach((wp, i) => {
      const icon = L.divIcon({
        className: 'wp-marker',
        html: `<div style="
          width:22px;height:22px;border-radius:50%;
          background:#71a0d622;border:2px solid #71a0d6;
          display:flex;align-items:center;justify-content:center;
          font-size:9px;color:#71a0d6;font-weight:bold;
          cursor:pointer;
        ">${i + 1}</div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      })
      const marker = L.marker([wp.lat, wp.lon], { icon })
        .bindTooltip(`WP${i + 1}<br>Alt: ${wp.alt_m}m`, { permanent: false })
        .addTo(wpLayerRef.current)
      marker.on('contextmenu', () => {
        setDrawnWps(prev => {
          const next = prev.filter((_, j) => j !== i)
          refreshWpMarkers(L, map, next)
          return next
        })
      })
    })

    // Draw planned path through waypoints
    if (plannedLayerRef.current) {
      plannedLayerRef.current.setLatLngs(wps.map(wp => [wp.lat, wp.lon]))
    }
  }, [])

  // Toggle draw mode
  const toggleDrawMode = useCallback(async () => {
    const L = (await import('leaflet')).default
    const map = mapRef.current
    if (!map) return
    const next = !drawMode
    setDrawMode(next)
    ;(map as any)._drawMode = next
    map.getContainer().style.cursor = next ? 'crosshair' : ''
    if (!next && drawnWps.length > 0) {
      refreshWpMarkers(L, map, drawnWps)
    }
  }, [drawMode, drawnWps, refreshWpMarkers])

  // Clear waypoints
  const clearWaypoints = useCallback(async () => {
    const L = (await import('leaflet')).default
    setDrawnWps([])
    refreshWpMarkers(L, mapRef.current, [])
  }, [refreshWpMarkers])

  // Upload waypoints via socket
  const uploadMission = useCallback(() => {
    if (!socket || drawnWps.length === 0) return
    socket.emit('gcs:mission_upload', { waypoints: drawnWps })
  }, [socket, drawnWps])

  // Toggle satellite/dark layers
  const toggleLayer = useCallback(async () => {
    const map = mapRef.current
    if (!map) return
    const next = !showSatellite
    setShowSatellite(next)
    const sat  = (map as any)._satelliteLayer
    const dark = (map as any)._darkLayer
    if (next) { dark.remove(); sat.addTo(map) }
    else       { sat.remove(); dark.addTo(map) }
  }, [showSatellite])

  // Home is where the flight started: the first sample with a position. The
  // default above is only where the map opens (it used to be the home for good,
  // so the H marker and geofence sat in Riyadh while the SITL flight was at
  // PX4's home in Zurich).
  const firstFix = useDroneStore(s => s.flightHome)
  const home: [number, number] = firstFix ?? [homeLat, homeLon]

  useEffect(() => {
    if (!mapReady || !firstFix) return
    homeMarkerRef.current?.setLatLng(firstFix)
    geofenceRef.current?.setLatLng(firstFix)
  }, [mapReady, firstFix])

  // Re-measure whenever the container changes size (grid reflow, panel toggle).
  useEffect(() => {
    const el = mapContainerRef.current
    if (!el || !mapReady) return
    const observer = new ResizeObserver(() => mapRef.current?.invalidateSize())
    observer.observe(el)
    return () => observer.disconnect()
  }, [mapReady])

  // Update drone marker from MAVLink telemetry (preferred) or store telemetry
  const droneLatLon = useMemo(() => {
    if (Number.isFinite(mavlinkTelemetry?.lat) && Number.isFinite(mavlinkTelemetry?.lon)) {
      return [mavlinkTelemetry.lat, mavlinkTelemetry.lon] as [number, number]
    }
    const latest = telemetryStream[telemetryStream.length - 1]
    if (!latest) return null
    // Use lat/lon if available, else convert NED
    if (Number.isFinite((latest as any).position?.lat) && Number.isFinite((latest as any).position?.lon)) {
      return [(latest as any).position.lat, (latest as any).position.lon] as [number, number]
    }
    return nedToLatLon(
      (latest as any).position?.north_m ?? 0,
      (latest as any).position?.east_m  ?? 0,
      home[0], home[1]
    )
  }, [mavlinkTelemetry, telemetryStream, home[0], home[1]]) // eslint-disable-line react-hooks/exhaustive-deps

  const droneHeading = useMemo(() => {
    if (mavlinkTelemetry?.yaw_deg != null) return mavlinkTelemetry.yaw_deg
    const latest = telemetryStream[telemetryStream.length - 1]
    return (latest as any)?.attitude?.yaw_deg ?? 0
  }, [mavlinkTelemetry, telemetryStream])

  // Update drone marker + flight path
  useEffect(() => {
    if (!mapRef.current || !droneMarkerRef.current || !droneLatLon) return
    droneMarkerRef.current.setLatLng(droneLatLon)
    // Rotate SVG arrow
    const el = mapRef.current.getContainer().querySelector('#drone-arrow')
    if (el) {
      (el as HTMLElement).style.transform = `rotate(${droneHeading}deg)`
    }
  }, [droneLatLon, droneHeading])

  // Update path polyline
  useEffect(() => {
    if (!pathLayerRef.current || !mapReady) return
    const pts = telemetryStream.map(t => {
      const p = (t as any).position
      if (Number.isFinite(p?.lat) && Number.isFinite(p?.lon)) return [p.lat, p.lon] as [number, number]
      return nedToLatLon(p?.north_m ?? 0, p?.east_m ?? 0, home[0], home[1])
    })
    pathLayerRef.current.setLatLngs(pts)
  }, [telemetryStream, mapReady, home[0], home[1]]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-center on drone when it moves
  useEffect(() => {
    if (!mapRef.current || !droneLatLon || telemetryStream.length < 2) return
    // Only follow if user hasn't panned away (simple heuristic: center within 500m)
    const center = mapRef.current.getCenter()
    const dist = mapRef.current.distance(center, { lat: droneLatLon[0], lng: droneLatLon[1] })
    if (dist > 500) mapRef.current.setView(droneLatLon, mapRef.current.getZoom())
  }, [droneLatLon]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className={`rounded-xl border border-border bg-card overflow-hidden flex flex-col ${className}`}>
      {/* Header toolbar */}
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5 flex-shrink-0">
        <div className="min-w-0">
          <span className="block text-sm font-medium text-foreground">Map</span>
          <span className="block text-2xs text-muted-foreground">
            {mavlinkTelemetry ? 'Live MAVLink position' : 'Position from the simulation telemetry'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {/* Draw mode toggle */}
          <button
            onClick={toggleDrawMode}
            title={drawMode ? 'Exit draw mode' : 'Draw waypoints (click map)'}
            className={`text-2xs font-medium px-2 py-1 rounded-md border transition-colors ${
              drawMode
                ? 'bg-info/10 border-info/30 text-info'
                : 'border-border text-muted-foreground hover:text-info'
            }`}
          >
            {drawMode ? 'Drawing…' : 'Draw waypoints'}
          </button>

          {/* Upload mission */}
          {drawnWps.length > 0 && (
            <>
              <button
                onClick={uploadMission}
                className="text-2xs font-medium px-2 py-1 rounded-md border border-info/30 text-info hover:bg-info/15 transition-colors"
              >
                Upload ({drawnWps.length})
              </button>
              <button
                onClick={clearWaypoints}
                className="text-2xs font-medium px-2 py-1 rounded-md border border-destructive/30 text-destructive hover:bg-destructive/15 transition-colors"
              >
                Clear
              </button>
            </>
          )}

          {/* Layer toggle */}
          <button
            onClick={toggleLayer}
            className="text-2xs font-medium px-2 py-1 rounded-md border border-border text-muted-foreground hover:text-foreground transition-colors"
          >
            {showSatellite ? 'Satellite' : 'Map'}
          </button>
        </div>
      </div>

      {/* Map container */}
      <div ref={mapContainerRef} className="flex-1 min-h-0" style={{ minHeight: 350 }} />

      {/* Legend */}
      <div className="flex items-center gap-4 px-3 py-1.5 border-t border-border/60 flex-shrink-0 text-2xs text-muted-foreground">
        <span className="flex items-center gap-1">
          <span className="w-6 h-0.5 bg-brand-400 inline-block" /> Path
        </span>
        <span className="flex items-center gap-1">
          <span className="w-6 h-0.5 bg-info inline-block border-dashed" style={{borderTop: '1px dashed #71a0d6', background: 'none'}} /> Planned
        </span>
        {geofenceRadius > 0 && (
          <span className="flex items-center gap-1">
            <span className="w-6 h-0.5 border-t border-destructive border-dashed inline-block" /> Geofence {geofenceRadius}m
          </span>
        )}
        <span className="ml-auto">Right-click WP to remove</span>
      </div>
    </div>
  )
}
