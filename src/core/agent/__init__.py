"""AI assistant foundation (placeholder).

This package will host the LangGraph-based Ops assistant (issue diagnosis,
cross-system Q&A, onboarding extraction, shift summaries). Nothing in the
platform imports it yet — it is intentionally dead code on this branch so the
LLM gateway config can land without any runtime risk.

Per repo convention every optional dependency (langchain / langgraph) is
guard-imported inside the modules; importing this package never raises just
because the agent extra isn't installed. Install deps with:

    pip install .[agent]
"""
