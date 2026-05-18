# Travel Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Vue3 + FastAPI + LangChain travel planning assistant with four specialized business agents and editable itinerary output.

**Architecture:** The backend exposes a small API surface and keeps external providers behind MCP-style client adapters. Four agents collaborate through deterministic, testable service boundaries, with LLM enhancement optional and fallback itinerary generation always available. The frontend is a Vite Vue app with a polished two-pane planning workspace and local itinerary editing.

**Tech Stack:** Python, FastAPI, Pydantic, pytest, LangChain, Vue 3, TypeScript, Vite.

---

### Task 1: Backend Core

**Files:**
- Create: `backend/app/models.py`
- Create: `backend/app/agents.py`
- Create: `backend/app/services.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_trip_planner.py`

- [ ] Write failing tests for parsing, planning, and recalculation.
- [ ] Implement Pydantic models and deterministic fallback data.
- [ ] Implement MCP-style Amap and Unsplash clients.
- [ ] Implement AttractionSearchAgent, WeatherQueryAgent, HotelAgent, and PlannerAgent.
- [ ] Wire FastAPI routes.
- [ ] Run `pytest`.

### Task 2: Frontend App

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/services/api.ts`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/styles.css`

- [ ] Create a Vue3 Vite app.
- [ ] Build a Figma-style travel dashboard input experience.
- [ ] Render itinerary days, map pins, weather, hotel, meals, and budget.
- [ ] Support deleting attractions and reordering them.
- [ ] Call backend recalculation after local edits.
- [ ] Run `npm install` and `npm run build`.
