// ============================================================
// eljazari.ai — Shared TypeScript Types
// ============================================================

// ── Pipeline Phases ──────────────────────────────────────────
export type PipelinePhase =
  | 'idle'
  | 'design'
  | 'review_design'
  | 'urdf_conversion'
  | 'code_generation'
  | 'safety_check'
  | 'review_code'
  | 'simulation'
  | 'monitoring'
  | 'complete'
  | 'error';

export type AgentStatus = 'idle' | 'running' | 'complete' | 'error' | 'waiting_approval';

// ── Drone Design ─────────────────────────────────────────────
export interface DroneDesign {
  id: string;
  project_id: string;
  name: string;
  description: string;
  design_tree: DroneDesignTree;
  urdf_path?: string;
  sdf_path?: string;
  created_at: string;
  updated_at: string;
}

export interface DroneDesignTree {
  /** Canonical topology, e.g. "quadcopter" | "hexacopter" | "fixed_wing". */
  topology?: string;
  frame: DroneFrame;
  motors: DroneMotor[];
  propellers: DronePropeller[];
  battery: DroneBattery;
  sensors: DroneSensor[];
  payload?: DronePayload;
  estimated_mass_kg: number;
  estimated_flight_time_min: number;
}

export interface DroneFrame {
  type: 'quadrotor' | 'hexarotor' | 'octorotor' | 'tricopter' | 'bicopter' | 'fixed_wing' | 'hybrid';
  wheelbase_mm: number;
  arm_length_mm: number;
  body_dimensions: { length_mm: number; width_mm: number; height_mm: number };
  material: string;
  mass_g: number;
}

export interface DroneMotor {
  id: string;
  position: { x_mm: number; y_mm: number; z_mm: number };
  kv_rating: number;
  kv?: number;           // alias for kv_rating used by some UI components
  max_thrust_g: number;
  direction: 'CW' | 'CCW';
}

export interface DronePropeller {
  motor_id: string;
  diameter_inch: number;
  pitch_inch: number;
  material: string;
}

export interface DroneBattery {
  cells: number;
  capacity_mah: number;
  c_rating: number;
  mass_g: number;
  voltage_nominal: number;
}

export interface DroneSensor {
  type: 'camera' | 'lidar' | 'ultrasonic' | 'gps' | 'imu' | 'barometer' | 'magnetometer';
  model?: string;
  position: { x_mm: number; y_mm: number; z_mm: number };
}

export interface DronePayload {
  description: string;
  mass_g: number;
  dimensions: { length_mm: number; width_mm: number; height_mm: number };
}

// ── Mission & Code ────────────────────────────────────────────
export interface Mission {
  id: string;
  project_id: string;
  description: string;
  constraints: MissionConstraints;
  generated_code?: string;
  safety_report?: SafetyReport;
  status: 'pending' | 'generating' | 'safety_check' | 'approved' | 'rejected';
}

export interface MissionConstraints {
  max_altitude_m: number;
  max_speed_ms: number;
  geofence_radius_m: number;
  min_battery_pct: number;
  max_duration_s: number;
}

export interface SafetyReport {
  passed: boolean;
  risk_level: 'low' | 'medium' | 'high' | 'critical';
  issues: SafetyIssue[];
  injected_protocols: string[];
  recommendations: string[];
}

export interface SafetyIssue {
  rule_id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  description: string;
  line_number?: number;
  suggestion: string;
}

// ── Simulation ────────────────────────────────────────────────
export interface SimulationRun {
  id: string;
  project_id: string;
  mission_id: string;
  iteration: number;
  status: 'pending' | 'running' | 'complete' | 'failed';
  passed?: boolean;      // convenience field mirroring evaluation.passed
  telemetry: TelemetryPoint[];
  semantic_observations: SemanticObservation[];
  evaluation?: SimulationEvaluation;
  started_at?: string;
  completed_at?: string;
}

export interface TelemetryPoint {
  timestamp_ms: number;
  position?: { lat: number; lon: number; alt_m: number; north_m: number; east_m: number };
  attitude?: { roll_deg: number; pitch_deg: number; yaw_deg: number };
  velocity?: { north_ms: number; east_ms: number; down_ms: number };
  battery?: { voltage_v: number; percent: number; remaining_s: number };
  flight_mode?: string;
  armed?: boolean;
  // Flat convenience fields used by UI components
  speed_ms?: number;
  battery_pct?: number;
}

export interface SemanticObservation {
  timestamp_ms: number;
  observation: string;
  category: 'position' | 'attitude' | 'battery' | 'mission_event' | 'error';
}

export interface SimulationEvaluation {
  passed: boolean;
  score: number;
  mission_objectives_met: string[];
  mission_objectives_failed: string[];
  feedback_for_code_agent: string;
}

// ── Monitor ───────────────────────────────────────────────────
export interface PerformanceReport {
  id: string;
  project_id: string;
  simulation_ids: string[];
  /** null when the run was blocked or rejected by the deterministic checks. */
  overall_score: number | null;
  metrics: PerformanceMetrics;
  recommendations: string[];
  generated_at: string;
  validation?: FlightValidation | null;
}

/** Deterministic gate verdict and the aircraft that actually flew. */
export interface FlightValidation {
  status: 'passed' | 'rejected_by_checks' | 'blocked' | 'unknown';
  reason: string | null;
  aircraft: {
    kind: 'generated' | 'reference_x500' | 'not_flown' | 'unknown';
    model_instance?: string | null;
    validation_scope?: string | null;
    simulated_total_mass_kg?: number | null;
    identity_attested: boolean;
  };
  /** "linux_namespaces" when the mission ran in the sandbox. */
  mission_isolation: string | null;
  acceptance_reasons: string[];
}

export interface PerformanceMetrics {
  mission_success_rate: number;
  avg_battery_efficiency: number;
  avg_position_accuracy_m: number;
  avg_mission_duration_s: number;
  avg_flight_duration_s?: number;   // alias used by monitor UI
  safety_incidents: number;
  code_iterations_needed: number;
}

// ── Project ───────────────────────────────────────────────────
export interface Project {
  id: string;
  name: string;
  prompt: string;
  phase: PipelinePhase;
  created_at: string;
  updated_at: string;
  design?: DroneDesign;
  missions: Mission[];
  simulations: SimulationRun[];
  performance_report?: PerformanceReport;
}

// ── WebSocket Events ──────────────────────────────────────────
export interface WSEvent {
  type: WSEventType;
  project_id: string;
  data: unknown;
  timestamp: string;
}

export type WSEventType =
  | 'phase_change'
  | 'agent_status'
  | 'design_update'
  | 'code_update'
  | 'safety_report'
  | 'simulation_telemetry'
  | 'simulation_complete'
  | 'monitor_report'
  | 'error'
  | 'hitl_request';

export interface HITLRequest {
  id: string;
  project_id: string;
  phase: 'design_review' | 'code_review';
  data: unknown;
  message: string;
}
