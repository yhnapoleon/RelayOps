"""Generic model-monitoring adapter for the public RelayOps demo."""
from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Optional, Any
import threading
import httpx

class MmpApiError(Exception):
    """MmpApiError: normalized monitoring data for RelayOps."""

    def __init__(self, message: str, status_code: Optional[int]=None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

@dataclass
class DriftResult:
    """DriftResult: normalized monitoring data for RelayOps."""
    action: str
    drifted: bool = False
    drift_details: Optional[str] = None
    cml_model_id: Optional[int] = None
    mmp_project_numeric_id: Optional[int] = None
    reason: str = ''
    fairness_risk: Optional[bool] = None
    fairness_risk_details: Optional[str] = None
    run_pending_approval: Optional[bool] = None
    run_pending_approval_details: Optional[str] = None
    run_pending_user_review: Optional[bool] = None
    run_pending_user_review_details: Optional[str] = None
    has_unapproved_exp_run: Optional[bool] = None
    has_unapproved_exp_run_details: Optional[str] = None
    latest_run_id: Optional[int] = None
    latest_run_pmetric_drifted: Optional[bool] = None
    latest_run_fmean_drifted: Optional[bool] = None
    latest_run_fmissing_drifted: Optional[bool] = None
    latest_run_drifted: Optional[bool] = None
    latest_run_approval_status: Optional[int] = None
    latest_run_is_deployed: Optional[bool] = None
    latest_run_approved: Optional[bool] = None
    latest_run_approved_at: Optional[datetime] = None
    drift_superseded_by_pending: bool = False

class MmpInterface:
    """Client for RelayOps' documented, synthetic model-monitoring protocol.

    This adapter intentionally does not implement an employer's API. The
    refresh_token argument is retained for call-site compatibility and unused.
    """

    def __init__(self, base_url: str, bearer_token: str, refresh_token: str = '', *,
                 verify_ssl: bool = True, ca_bundle=None, timeout: float = 30.0):
        self.base_url = (base_url or '').rstrip('/')
        self._bearer = bearer_token or ''
        self._verify = ca_bundle if verify_ssl and ca_bundle else verify_ssl
        self._timeout = timeout
        self._cache_lock = threading.RLock()
        self.clear_cache()

    def is_configured(self):
        return bool(self.base_url and self._bearer)

    def clear_cache(self):
        with self._cache_lock:
            self._shallow_cache = None
            self._project_cache = {}

    def _get(self, path):
        if not path.startswith('/api/') or '..' in path or '://' in path:
            raise MmpApiError('Only relative /api/ monitor routes are supported')
        try:
            with httpx.Client(verify=self._verify, timeout=self._timeout) as client:
                response = client.get(self.base_url + path, headers={
                    'Authorization': 'Bearer ' + self._bearer, 'Accept': 'application/json'})
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise MmpApiError(f'Monitor returned HTTP {exc.response.status_code}', exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MmpApiError('Monitor request failed or returned invalid JSON') from exc

    def list_projects_shallow(self):
        with self._cache_lock:
            if self._shallow_cache is not None:
                return self._shallow_cache
            payload = self._get('/api/projects')
            if not isinstance(payload, dict) or not isinstance(payload.get('projects'), list):
                raise MmpApiError('Expected a projects array')
            index = {}
            for project in payload['projects']:
                if not isinstance(project, dict) or not project.get('name'):
                    raise MmpApiError('Invalid project entry')
                index[project['name']] = {
                    'id': int(project['id']), 'business_name': project.get('display_name', project['name']),
                    'models': [{'id': m['id'], 'name': m['name'], 'is_production': bool(m.get('is_production'))}
                               for m in project.get('models', [])]}
            self._shallow_cache = index
            return index

    def get_project(self, project_id):
        pid = int(project_id)
        with self._cache_lock:
            if pid not in self._project_cache:
                payload = self._get(f'/api/projects/{pid}')
                if not isinstance(payload, dict) or not isinstance(payload.get('models'), list):
                    raise MmpApiError('Expected a project with a models array')
                # model_name is a normalized application field used by the picker.
                self._project_cache[pid] = {**payload, 'models': [
                    self._normalize_model(m) for m in payload['models']]}
            return self._project_cache[pid]

    @staticmethod
    def _normalize_model(model):
        """Translate the public wire signals into application presentation fields."""
        signals = model.get('signals') or {}
        mapping = {
            'model_drifted': ('drifted', 'drift_details'),
            'fairness_risk': ('fairness_risk', 'fairness_risk_details'),
            'run_pending_approval': ('run_pending_approval', 'run_pending_approval_details'),
            'run_pending_user_review': ('run_pending_user_review', 'run_pending_user_review_details'),
            'has_unapproved_exp_run': ('has_unapproved_exp_run', 'has_unapproved_exp_run_details'),
        }
        attention = {name: {'status': signals[key], 'description': signals.get(detail, '')}
                     for name, (key, detail) in mapping.items() if isinstance(signals.get(key), bool)}
        return {**model, 'model_name': model['name'], 'signals': signals,
                'attention_required': attention}

    def get_model_raw(self, repo_name, model_name):
        project = self.list_projects_shallow().get(repo_name)
        if not project:
            raise LookupError('Project binding not found')
        for model in self.get_project(project['id'])['models']:
            if model['name'] == model_name:
                return model
        raise LookupError('Model binding not found')

    def get_raw(self, path):
        return self._get(path)

    def check_drift(self, repo_name, model_name, job):
        if not self.is_configured():
            return DriftResult(action='skipped', reason='Model monitor is not configured')
        try:
            model = self.get_model_raw(repo_name, model_name)
            project = self.list_projects_shallow()[repo_name]
            signals = model.get('signals', {})
            if not isinstance(signals, dict) or not isinstance(signals.get('drifted'), bool):
                return DriftResult(action='inconclusive', reason='Monitor has no valid drift status')
            allowed = {f.name for f in fields(DriftResult)} - {'action', 'reason', 'cml_model_id', 'mmp_project_numeric_id'}
            values = {key: value for key, value in signals.items() if key in allowed}
            if isinstance(values.get('latest_run_approved_at'), str):
                stamp = datetime.fromisoformat(values['latest_run_approved_at'].replace('Z', '+00:00'))
                values['latest_run_approved_at'] = stamp.replace(tzinfo=None)
            return DriftResult(action='create_issue' if signals['drifted'] else 'close_issue',
                cml_model_id=int(model['id']), mmp_project_numeric_id=project['id'], **values)
        except LookupError as exc:
            return DriftResult(action='skipped', reason=str(exc))
        except (MmpApiError, ValueError, TypeError, AttributeError) as exc:
            return DriftResult(action='inconclusive', reason=str(exc))


def describe_approval_status(code):
    """Small demo lifecycle; these values are defined only for this repository."""
    return {0: 'Draft', 1: 'Pending review', 2: 'Approved'}.get(code, f'Unknown ({code})')


def approval_status_legend():
    return {0: 'Draft', 1: 'Pending review', 2: 'Approved'}
