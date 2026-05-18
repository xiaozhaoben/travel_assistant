# Attraction Recommendation Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve attraction candidate quality by adding a deterministic scoring and ranking layer on top of the existing Amap MCP POI search.

**Architecture:** Keep the existing `AttractionSearchAgent` and `AmapMCPClient` boundaries. Add an `AttractionRecommendationService` in `backend/app/services.py` that filters, scores, de-duplicates, ranks, and lightly diversifies POI candidates before they reach the planner. Tests use existing fake MCP callers to verify generic and noisy results are pushed out by real, preference-matching attractions.

**Tech Stack:** Python 3, FastAPI backend, Pydantic models, pytest, existing `mcp` stdio adapter.

---

## File Structure

- Modify `backend/app/services.py`: add `AttractionRecommendationService`, wire it into `AmapMCPClient.search_pois`, and keep existing fallback behavior.
- Modify `backend/tests/test_trip_planner.py`: add focused tests for generic POI demotion, `must_visit` boost, `avoid_places` filtering, and rating/preference ranking.
- No frontend changes in this plan.

---

### Task 1: Add Recommendation Service Tests

**Files:**
- Modify: `backend/tests/test_trip_planner.py`

- [ ] **Step 1: Add failing tests for ranking and filtering**

Add tests near the existing Amap POI tests:

```python
def test_recommendation_service_prioritizes_real_attractions_over_generic_names():
    from app.services import AttractionRecommendationService

    center = Location(longitude=113.26, latitude=23.13)
    generic = Attraction(
        id="generic",
        name="骞垮窞鍘嗗彶鏂囧寲鏅偣",
        category="鏃呮父鏅偣",
        address="骞垮窞甯傝秺绉€鍖?,
        location=center,
        visit_duration_minutes=90,
        description="妯℃澘鍖栧湴鐐?,
        ticket_price=0,
        rating=4.8,
    )
    real = Attraction(
        id="real",
        name="闄堝绁?,
        category="鍘嗗彶鏂囧寲;鍗氱墿棣?,
        address="骞垮窞甯傝崝婀惧尯涓北涓冭矾",
        location=Location(longitude=113.2466, latitude=23.1317),
        visit_duration_minutes=120,
        description="宀崡绁犲爞寤虹瓚浠ｈ〃",
        ticket_price=10,
        rating=4.6,
    )

    ranked = AttractionRecommendationService().rank(
        [generic, real],
        city="骞垮窞",
        preferences=["鍘嗗彶鏂囧寲"],
        limit=2,
    )

    assert [item.name for item in ranked] == ["闄堝绁?, "骞垮窞鍘嗗彶鏂囧寲鏅偣"]
```

```python
def test_recommendation_service_filters_avoid_places_and_boosts_must_visit():
    from app.services import AttractionRecommendationService

    chen = Attraction(
        id="chen",
        name="闄堝绁?,
        category="鍘嗗彶鏂囧寲",
        address="骞垮窞甯傝崝婀惧尯",
        location=Location(longitude=113.2466, latitude=23.1317),
        visit_duration_minutes=120,
        description="宀崡寤虹瓚",
        ticket_price=10,
        rating=4.5,
    )
    sha = Attraction(
        id="sha",
        name="娌欓潰宀?,
        category="鍘嗗彶鏂囧寲;琛楀尯",
        address="骞垮窞甯傝崝婀惧尯",
        location=Location(longitude=113.2384, latitude=23.1092),
        visit_duration_minutes=120,
        description="杩戜唬寤虹瓚琛楀尯",
        ticket_price=0,
        rating=4.4,
    )
    avoided = Attraction(
        id="avoid",
        name="骞垮窞濉?,
        category="鍩庡競鍦版爣",
        address="骞垮窞甯傛捣鐝犲尯",
        location=Location(longitude=113.3307, latitude=23.1066),
        visit_duration_minutes=120,
        description="鍩庡競鍦版爣",
        ticket_price=150,
        rating=4.9,
    )

    ranked = AttractionRecommendationService().rank(
        [chen, sha, avoided],
        city="骞垮窞",
        preferences=["鍘嗗彶鏂囧寲"],
        limit=3,
        must_visit=["娌欓潰宀?],
        avoid_places=["骞垮窞濉?],
    )

    assert [item.name for item in ranked] == ["娌欓潰宀?, "闄堝绁?]
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest backend/tests/test_trip_planner.py::test_recommendation_service_prioritizes_real_attractions_over_generic_names backend/tests/test_trip_planner.py::test_recommendation_service_filters_avoid_places_and_boosts_must_visit -q
```

Expected: FAIL with import error for `AttractionRecommendationService`.

---

### Task 2: Implement AttractionRecommendationService

**Files:**
- Modify: `backend/app/services.py`

- [ ] **Step 1: Add rating to Attraction model usage**

The existing `Attraction` model already allows `rating`, and `_poi_to_attraction` already fills it from MCP detail when available. No model migration is required.

- [ ] **Step 2: Add the service class**

Add `AttractionRecommendationService` after `AmapStdioMCPToolCaller` and before `AmapMCPClient`:

```python
class AttractionRecommendationService:
    generic_terms = {"历史文化景点", "城市公园", "特色街区", "观景台", "美食街"}
    excluded_terms = {"酒店", "停车场", "游客中心", "入口", "出口", "厕所"}
    quality_terms = {"博物馆", "风景名胜", "旅游景点", "古镇", "故居", "遗址", "公园", "街区"}
    preference_terms = {"历史文化": {"历史", "文化", "博物馆", "故居", "遗址", "古镇"}}

    def rank(
        self,
        attractions: list[Attraction],
        city: str,
        preferences: Iterable[str],
        limit: int,
        must_visit: Iterable[str] | None = None,
        avoid_places: Iterable[str] | None = None,
    ) -> list[Attraction]:
        scored = []
        for attraction in attractions:
            score = self._score(attraction, city, preferences, must_visit, avoid_places)
            if score is not None:
                scored.append((score, attraction))
        return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]
```

Implementation requirements:

- `rank()` accepts `attractions`, `city`, `preferences`, `limit`, `must_visit=None`, and `avoid_places=None`.
- Hard-filter avoided places, excluded facilities, and sub-POIs.
- Score preference matches, quality categories, rating, complete metadata, must-visit boost, and generic name penalty.
- De-duplicate by normalized attraction name.
- Sort by score descending.
- Apply light spatial diversity while preserving high-scoring items.

- [ ] **Step 3: Run focused tests**

Run:

```bash
pytest backend/tests/test_trip_planner.py::test_recommendation_service_prioritizes_real_attractions_over_generic_names backend/tests/test_trip_planner.py::test_recommendation_service_filters_avoid_places_and_boosts_must_visit -q
```

Expected: PASS.

---

### Task 3: Wire Ranking into AmapMCPClient

**Files:**
- Modify: `backend/app/services.py`
- Modify: `backend/app/agents.py`

- [ ] **Step 1: Update AmapMCPClient constructor**

Add optional `recommendation_service`:

```python
def __init__(self, api_key: str | None = None, mcp_caller=None, recommendation_service=None):
    settings = get_settings()
    self.api_key = api_key if api_key is not None else settings.amap_api_key or os.getenv("AMAP_API_KEY") or os.getenv("AMAP_MAPS_API_KEY")
    self.mcp_caller = mcp_caller or (AmapStdioMCPToolCaller(self.api_key) if self.api_key else None)
    self.recommendation_service = recommendation_service or AttractionRecommendationService()
```

- [ ] **Step 2: Update search_pois signature**

Change:

```python
def search_pois(self, city: str, keywords: Iterable[str], limit: int = 9) -> List[Attraction]:
```

to:

```python
def search_pois(
    self,
    city: str,
    keywords: Iterable[str],
    limit: int = 9,
    must_visit: Iterable[str] | None = None,
    avoid_places: Iterable[str] | None = None,
) -> List[Attraction]:
```

- [ ] **Step 3: Rank MCP and fallback candidates**

Use:

```python
return self.recommendation_service.rank(
    ordered,
    city=city,
    preferences=list(keywords),
    limit=limit,
    must_visit=must_visit,
    avoid_places=avoid_places,
)
```

Keep `_select_spatially_diverse_attractions` as a helper or delegate its behavior to the new service.

- [ ] **Step 4: Pass must/avoid data from AttractionSearchAgent**

In `AttractionSearchAgent.run`, call:

```python
attractions = self.amap.search_pois(
    requirement.city,
    search_queries,
    limit=requirement.days * 3,
    must_visit=requirement.must_visit,
    avoid_places=requirement.avoid_places,
)
```

- [ ] **Step 5: Run existing POI tests**

Run:

```bash
pytest backend/tests/test_trip_planner.py -q
```

Expected: PASS.

---

### Task 4: Add Integration Coverage for Amap Ranking

**Files:**
- Modify: `backend/tests/test_trip_planner.py`

- [ ] **Step 1: Add fake MCP test for score ordering**

Add:

```python
def test_amap_client_ranks_specific_relevant_pois_ahead_of_generic_high_rating_results():
    class RankingMCPCaller:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool_name, arguments):
            self.calls.append({"tool_name": tool_name, "arguments": arguments})
            return {
                "pois": [
                    {
                        "id": "generic",
                        "name": "骞垮窞鍘嗗彶鏂囧寲鏅偣",
                        "type": "鏃呮父鏅偣",
                        "address": "骞垮窞甯傝秺绉€鍖?,
                        "location": "113.2600,23.1300",
                        "biz_ext": {"rating": "4.9"},
                    },
                    {
                        "id": "chen",
                        "name": "闄堝绁?,
                        "type": "绉戞暀鏂囧寲鏈嶅姟;鍗氱墿棣?,
                        "address": "骞垮窞甯傝崝婀惧尯涓北涓冭矾",
                        "location": "113.2466,23.1317",
                        "biz_ext": {"rating": "4.6"},
                    },
                ]
            }

    amap = AmapMCPClient(api_key="amap-key", mcp_caller=RankingMCPCaller())

    pois = amap.search_pois("骞垮窞", ["骞垮窞鍘嗗彶鏂囧寲鏅偣"], limit=2)

    assert [poi.name for poi in pois] == ["闄堝绁?, "骞垮窞鍘嗗彶鏂囧寲鏅偣"]
```

- [ ] **Step 2: Run the new integration test**

Run:

```bash
pytest backend/tests/test_trip_planner.py::test_amap_client_ranks_specific_relevant_pois_ahead_of_generic_high_rating_results -q
```

Expected: PASS.

---

### Task 5: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run backend tests**

Run:

```bash
pytest backend/tests/test_trip_planner.py -q
```

Expected: `45+ passed`.

- [ ] **Step 2: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: build succeeds.

- [ ] **Step 3: Review diff**

Run:

```bash
git diff -- backend/app/services.py backend/app/agents.py backend/tests/test_trip_planner.py
```

Expected: only recommendation quality changes and tests.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add backend/app/services.py backend/app/agents.py backend/tests/test_trip_planner.py docs/superpowers/plans/2026-05-18-attraction-recommendation-quality.md
git commit -m "feat: 浼樺寲鏅偣鎺ㄨ崘璐ㄩ噺"
```
