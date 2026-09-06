"""
Services layer — business logic modules.

Module-level functions; no Service classes. Sessions are passed in by
the route boundary (api.deps.db.get_session); these modules never call
session.commit() unless they manage their own transactions internally.

- user_service: user lookup, role resolution, account initialization
- project_service: Project CRUD with access control
- product_service: Product CRUD + check-now + version helpers
- app_service: Application + recovery-scenario CRUD
- job_service: Job + failure-scenario CRUD
- issue_service: Issue lifecycle (create/dispatch, actions, handover approve/reject, update)
- handover_service: product handover readiness evaluation + version workflow
- product_version_service: product-version lifecycle helpers
- audit_service: serializers + log_audit helper
- support_group_service: support-group registry + project bindings
- version_number_utils: semver-ish helpers
"""
