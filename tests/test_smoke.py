import asyncio
import pytest
from httpx import AsyncClient
from app.server_ws import app

@pytest.mark.asyncio
async def test_health_and_openapi():
    async with AsyncClient(app=app, base_url='http://test') as ac:
        r = await ac.get('/api/health')
        assert r.status_code == 200
        r2 = await ac.get('/openapi.json')
        assert r2.status_code == 200

@pytest.mark.asyncio
async def test_ui_endpoints():
    async with AsyncClient(app=app, base_url='http://test') as ac:
        for path in ['/api/ui/execution','/api/ui/pnl','/api/ui/exposure','/api/ui/control-state','/api/ui/approvals','/api/ui/limits','/api/ui/universe']:
            r = await ac.get(path)
            assert r.status_code == 200
        r = await ac.get('/api/opportunities')
        assert r.status_code == 200

@pytest.mark.asyncio
async def test_status_endpoints():
    async with AsyncClient(app=app, base_url='http://test') as ac:
        for path in ['/api/ui/status/overview','/api/ui/status/components','/api/ui/status/slo']:
            r = await ac.get(path)
            assert r.status_code == 200
