"""Initial schema — 7 tables

Revision ID: 0001
Revises:
Create Date: 2026-03-14 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── ENUM definitions (values kept here so create_table can reference them) ──
# create_type=False → we create them manually with IF NOT EXISTS below
_project_status   = postgresql.ENUM("draft","designing","coding","simulating","testing","monitoring","completed","failed",   name="project_status",   create_type=False)
_design_status    = postgresql.ENUM("generated","validated","converted","approved","rejected",                                name="design_status",    create_type=False)
_code_type        = postgresql.ENUM("mission","control","navigation","safety",                                               name="code_type",        create_type=False)
_simulator_type   = postgresql.ENUM("gazebo","airsim","sitl",                                                               name="simulator_type",   create_type=False)
_simulation_status= postgresql.ENUM("pending","running","completed","failed","crashed",                                      name="simulation_status",create_type=False)
_log_type         = postgresql.ENUM("position","attitude","battery","motor","error","event",                                 name="log_type",         create_type=False)
_analysis_type    = postgresql.ENUM("performance","failure","recommendation","comparison",                                   name="analysis_type",    create_type=False)
_report_severity  = postgresql.ENUM("info","warning","critical",                                                             name="report_severity",  create_type=False)
_agent_log_status = postgresql.ENUM("success","error","timeout",                                                             name="agent_log_status", create_type=False)


def _create_enums(bind) -> None:
    """Create ENUM types using DO blocks (idempotent)."""
    statements = [
        "DO $$ BEGIN CREATE TYPE project_status    AS ENUM ('draft','designing','coding','simulating','testing','monitoring','completed','failed');   EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE design_status     AS ENUM ('generated','validated','converted','approved','rejected');                               EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE code_type         AS ENUM ('mission','control','navigation','safety');                                              EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE simulator_type    AS ENUM ('gazebo','airsim','sitl');                                                               EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE simulation_status AS ENUM ('pending','running','completed','failed','crashed');                                      EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE log_type          AS ENUM ('position','attitude','battery','motor','error','event');                                 EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE analysis_type     AS ENUM ('performance','failure','recommendation','comparison');                                   EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE report_severity   AS ENUM ('info','warning','critical');                                                             EXCEPTION WHEN duplicate_object THEN null; END $$;",
        "DO $$ BEGIN CREATE TYPE agent_log_status  AS ENUM ('success','error','timeout');                                                             EXCEPTION WHEN duplicate_object THEN null; END $$;",
    ]
    for stmt in statements:
        bind.execute(sa.text(stmt))


def upgrade() -> None:
    bind = op.get_bind()
    _create_enums(bind)

    # 1. projects
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("status", _project_status, nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_index("ix_projects_status",  "projects", ["status"])

    # 2. designs
    op.create_table(
        "designs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("design_tree",          postgresql.JSONB, nullable=True),
        sa.Column("design_params",        postgresql.JSONB, nullable=True),
        sa.Column("performance_estimate", postgresql.JSONB, nullable=True),
        sa.Column("urdf_file_path", sa.String(512), nullable=True),
        sa.Column("sdf_file_path",  sa.String(512), nullable=True),
        sa.Column("stl_files",      postgresql.JSONB, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", _design_status, nullable=False, server_default="generated"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_designs_project_id", "designs", ["project_id"])
    op.create_index("ix_designs_status",     "designs", ["status"])

    # 3. generated_codes
    op.create_table(
        "generated_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("design_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("designs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_content", sa.Text, nullable=False),
        sa.Column("code_type", _code_type, nullable=False, server_default="mission"),
        sa.Column("gsce_config",       postgresql.JSONB, nullable=True),
        sa.Column("safety_validated",  sa.Boolean, nullable=False, server_default="false"),
        sa.Column("safety_report",     postgresql.JSONB, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_generated_codes_project_id", "generated_codes", ["project_id"])
    op.create_index("ix_generated_codes_design_id",  "generated_codes", ["design_id"])
    op.create_index("ix_generated_codes_code_type",  "generated_codes", ["code_type"])

    # 4. simulations
    op.create_table(
        "simulations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("generated_codes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("simulator", _simulator_type,    nullable=False, server_default="sitl"),
        sa.Column("status",    _simulation_status, nullable=False, server_default="pending"),
        sa.Column("config",               postgresql.JSONB, nullable=True),
        sa.Column("semantic_observations",postgresql.JSONB, nullable=True),
        sa.Column("raw_telemetry",        postgresql.JSONB, nullable=True),
        sa.Column("success_rate",    sa.Float,   nullable=True),
        sa.Column("completeness",    sa.Float,   nullable=True),
        sa.Column("duration_seconds",sa.Float,   nullable=True),
        sa.Column("iteration", sa.Integer, nullable=False, server_default="1"),
        sa.Column("started_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_simulations_project_id", "simulations", ["project_id"])
    op.create_index("ix_simulations_code_id",    "simulations", ["code_id"])
    op.create_index("ix_simulations_status",     "simulations", ["status"])

    # 5. flight_logs
    op.create_table(
        "flight_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("simulation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("simulations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("log_type",     _log_type,     nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger, nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_flight_logs_simulation_id", "flight_logs", ["simulation_id"])
    op.create_index("ix_flight_logs_log_type",      "flight_logs", ["log_type"])
    op.create_index("ix_flight_logs_timestamp_ms",  "flight_logs", ["timestamp_ms"])

    # 6. monitor_reports
    op.create_table(
        "monitor_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("simulation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("simulations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_type",  _analysis_type,   nullable=False),
        sa.Column("findings",       postgresql.JSONB,  nullable=True),
        sa.Column("recommendations",postgresql.JSONB,  nullable=True),
        sa.Column("severity", _report_severity, nullable=False, server_default="info"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_monitor_reports_project_id",    "monitor_reports", ["project_id"])
    op.create_index("ix_monitor_reports_simulation_id", "monitor_reports", ["simulation_id"])
    op.create_index("ix_monitor_reports_severity",      "monitor_reports", ["severity"])

    # 7. agent_logs
    op.create_table(
        "agent_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_name",     sa.String(128), nullable=False),
        sa.Column("action",         sa.String(256), nullable=False),
        sa.Column("input_summary",  sa.Text,        nullable=True),
        sa.Column("output_summary", sa.Text,        nullable=True),
        sa.Column("tokens_used",    sa.Integer,     nullable=True),
        sa.Column("duration_ms",    sa.Integer,     nullable=True),
        sa.Column("status", _agent_log_status, nullable=False, server_default="success"),
        sa.Column("error_message",  sa.Text,        nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_logs_project_id", "agent_logs", ["project_id"])
    op.create_index("ix_agent_logs_agent_name", "agent_logs", ["agent_name"])
    op.create_index("ix_agent_logs_status",     "agent_logs", ["status"])
    op.create_index("ix_agent_logs_created_at", "agent_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("agent_logs")
    op.drop_table("monitor_reports")
    op.drop_table("flight_logs")
    op.drop_table("simulations")
    op.drop_table("generated_codes")
    op.drop_table("designs")
    op.drop_table("projects")

    for name in [
        "agent_log_status", "report_severity", "analysis_type",
        "log_type", "simulation_status", "simulator_type",
        "code_type", "design_status", "project_status",
    ]:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))
