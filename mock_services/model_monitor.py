"""Synthetic model signals for the public RelayOps demo. In-memory and resettable."""
from copy import deepcopy
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title='RelayOps demo model monitor')
_project = {'id': 1, 'name': 'inventory-risk-demo', 'display_name': 'Inventory demo', 'models': [
    {'id': 1, 'name': 'agg-v2', 'is_production': True, 'signals': {'drifted': False,
     'drift_details': 'Synthetic distribution check', 'fairness_risk': False,
     'run_pending_approval': False, 'run_pending_user_review': False, 'has_unapproved_exp_run': False}}
]}


def authorize(value):
    if value != 'Bearer relayops-demo-monitor-token':
        raise HTTPException(401, 'Use the documented local demo token')


@app.get('/api/projects')
def projects(authorization: str = Header('')):
    authorize(authorization)
    return {'projects': [deepcopy(_project)]}


@app.get('/api/projects/{project_id}')
def project(project_id: int, authorization: str = Header('')):
    authorize(authorization)
    if project_id != 1:
        raise HTTPException(404, 'Demo project not found')
    return deepcopy(_project)


class Signals(BaseModel):
    drifted: bool = False
    fairness_risk: bool = False
    run_pending_approval: bool = False
    run_pending_user_review: bool = False


@app.post('/api/demo/signals')
def signals(value: Signals, authorization: str = Header('')):
    authorize(authorization)
    _project['models'][0]['signals'].update(value.model_dump())
    return deepcopy(_project['models'][0]['signals'])


@app.get('/health')
def health():
    return {'status': 'ok', 'synthetic': True}
