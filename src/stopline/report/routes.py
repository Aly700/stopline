from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pymongo.errors import PyMongoError

from .build import build_report
from .render import render_report


def report_routes(db) -> APIRouter:
    """Mount explicitly with app.include_router(report_routes(db))."""
    router = APIRouter()

    def read(run_id):
        try:
            return build_report(db, run_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Run not found") from None
        except PyMongoError:
            raise HTTPException(status_code=503, detail="Stored run is temporarily unavailable") from None

    @router.get("/report/{run_id}", response_class=HTMLResponse)
    def page(run_id: str):
        return render_report(read(run_id))

    @router.get("/api/report/{run_id}")
    def data(run_id: str):
        return read(run_id)

    return router
