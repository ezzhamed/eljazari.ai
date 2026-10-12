"""eljazari.ai - Files Router (URDF / code / PDF download)."""

import logging
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from db.session import get_db
from middleware.auth import AuthUser
from middleware.rate_limit import file_download_limit
from repositories.design import DesignRepository
from repositories.generated_code import GeneratedCodeRepository
from repositories.monitor_report import MonitorReportRepository
from repositories.project import ProjectRepository
from services.project_access import parse_project_uuid, require_project_access

logger = logging.getLogger(__name__)
router = APIRouter()
_STORAGE = Path(settings.storage_path).resolve()


def _safe_storage_path(path_value: str | Path) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = _STORAGE / candidate
    target = candidate.resolve()
    if target != _STORAGE and _STORAGE not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target


def _find_latest_project_file(project_id: str, pattern: str) -> Path | None:
    project_dir = (_STORAGE / project_id).resolve()
    if not project_dir.exists():
        return None
    matches = sorted(
        project_dir.rglob(pattern),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _normalize_recommendations(items) -> list[str]:
    recommendations: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("recommendation") or item.get("description")
            if text:
                recommendations.append(str(text))
        elif item:
            recommendations.append(str(item))
    return recommendations


@router.get("/{project_id}/files/urdf", response_class=FileResponse)
@file_download_limit
async def download_urdf(
    request: Request,
    response: Response,
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
):
    """Download the generated URDF file for the drone design."""
    project = await require_project_access(db, project_id, user)
    repo = DesignRepository(db)
    design = await repo.get_latest_for_project(project.id)

    urdf_file: Path | None = None
    if design and design.urdf_file_path:
        urdf_file = _safe_storage_path(design.urdf_file_path)
    if urdf_file is None or not urdf_file.exists():
        urdf_file = _find_latest_project_file(str(project.id), "*.urdf")
    if urdf_file is None or not urdf_file.exists():
        raise HTTPException(status_code=404, detail="URDF file not found")

    return FileResponse(
        path=urdf_file,
        media_type="application/xml",
        filename=f"drone_{str(project.id)[:8]}.urdf",
    )


@router.get("/{project_id}/files/code")
@file_download_limit
async def download_code(
    request: Request,
    response: Response,
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download the generated mission Python script."""
    project = await require_project_access(db, project_id, user)
    repo = GeneratedCodeRepository(db)
    record = await repo.get_latest_for_project(project.id)

    code: str | None = record.code_content if record else None
    if not code:
        candidate = _find_latest_project_file(str(project.id), "mission*.py")
        if candidate and candidate.exists():
            safe_path = _safe_storage_path(candidate)
            code = safe_path.read_text(encoding="utf-8")
    if not code:
        raise HTTPException(status_code=404, detail="Code file not found")

    return StreamingResponse(
        iter([code.encode("utf-8")]),
        media_type="text/x-python",
        headers={
            "Content-Disposition": f'attachment; filename="mission_{str(project.id)[:8]}.py"',
        },
    )


@router.get("/{project_id}/files/report")
@file_download_limit
async def download_report(
    request: Request,
    response: Response,
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Generate and download a mission PDF report."""
    project = await require_project_access(db, project_id, user)
    try:
        pdf_bytes = await _generate_pdf_report(str(project.id), db)
    except Exception as exc:
        logger.exception("download_report PDF generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}") from exc

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="report_{str(project.id)[:8]}.pdf"',
        },
    )


async def _generate_pdf_report(project_id: str, db: AsyncSession) -> bytes:
    """Build a minimal mission report PDF."""
    lines: list[str] = [f"eljazari.ai Mission Report - Project {project_id}", ""]

    project_repo = ProjectRepository(db)
    project = await project_repo.get_or_raise(parse_project_uuid(project_id))
    lines.append(f"Name:   {project.name}")
    lines.append(f"Prompt: {project.prompt}")
    lines.append(f"Status: {project.status}")
    lines.append("")

    report_repo = MonitorReportRepository(db)
    report = await report_repo.get_latest_for_project(project.id)
    if report:
        findings = report.findings or {}
        lines.append(f"Overall Score: {findings.get('overall_score', 'N/A')}")
        for rec in _normalize_recommendations(report.recommendations):
            lines.append(f"  - {rec}")

    try:
        from reportlab.pdfgen import canvas as rl_canvas

        buf = BytesIO()
        canvas = rl_canvas.Canvas(buf)
        y = 800
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawString(50, y, lines[0])
        canvas.setFont("Helvetica", 10)
        for line in lines[2:]:
            y -= 18
            if y < 50:
                canvas.showPage()
                y = 800
            canvas.drawString(50, y, line)
        canvas.save()
        return buf.getvalue()
    except ImportError:
        pass

    content = "\n".join(lines)
    pdf = (
        "%PDF-1.4\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        f"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Contents 4 0 R/Parent 2 0 R>>endobj\n"
        f"4 0 obj<</Length {len(content)}>>\nstream\n"
        f"BT /F1 12 Tf 50 750 Td ({content[:500]}) Tj ET\n"
        "endstream\nendobj\n"
        "xref\n0 5\n"
        "trailer<</Root 1 0 R/Size 5>>\n"
        "%%EOF\n"
    )
    return pdf.encode("utf-8")
