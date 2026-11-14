"""An optional router; mounting it performs no network work."""
from fastapi import APIRouter

from .core import failure, service_for


def search_routes(db, *, search=None):
    router = APIRouter()
    service = search if search is not None else service_for(db)

    @router.get('/api/ask')
    def ask(q: str = '', run_id: str | None = None):
        try:
            return service.ask(run_id, q)
        except Exception:
            return failure('Audit search is unavailable; try again.')

    return router
