# Praetor Implementation Roadmap

## Objective

Build the full Praetor system as a modular decision-intelligence operating layer across Cardo Praevisio.

Praetor must not be implemented as a scanner-only assistant or a collection of disconnected AI widgets. The first build should create the foundation that every future module plugs into.

## Full Module Roadmap

Praetor will eventually cover:

- Global Praetor assistant
- Scanner AI
- Research AI
- Journal AI
- Risk AI
- Wealth Management AI
- Memory graph
- Investment committee
- Daily briefings
- Discovery engine
- Alerts
- Text notifications
- Personal playbook
- Trade plans
- On-demand chart generation

## Architecture Principle

Every Praetor feature should use the same core foundations:

- shared context system
- AI provider abstraction
- deterministic analytics first
- evidence packets
- memory writes
- interaction logging
- modular domain tools
- graceful fallback when AI/data is unavailable

Do not hardcode page-specific AI logic that must be rebuilt later.

## Phase 1: Praetor Foundation + First Vertical Slice

Goal: create the foundation for the full Praetor system while shipping one working scanner vertical slice.

### Included

- Praetor core service
- AI provider abstraction
- OpenAI provider first
- shared context object
- memory schema foundation
- interaction logging
- scanner Ask Praetor endpoint
- trade plan generation endpoint
- playbook foundation endpoint
- first scanner UI actions:
  - Ask Praetor
  - Save Trade Plan

### Out of Scope

- real-time price monitoring
- SMS/text notifications
- full memory graph intelligence
- investment committee voting
- daily briefings
- wealth management AI
- full chart generation

Phase 1 should prove the architecture and page integration pattern.

## Phase 2: Scanner Trade Plan + Alert Foundation

### Included

- persistent trade plan lifecycle
- aggressive / balanced / conservative plan generation
- entry, stop, targets, chase threshold
- plan status
- manual trigger alerts
- in-app alert center
- setup deterioration checks
- initial playbook comparison

## Phase 3: Journal AI + Playbook Learning

### Included

- AI post-trade reviews
- mistake tagging
- setup outcome analysis
- playbook rules
- expectancy by setup
- behavioral pattern detection
- repeated mistake detection
- Praetor direct challenges based on journal evidence

## Phase 4: Risk AI + Portfolio Context

### Included

- portfolio exposure context
- concentration warnings
- daily loss and max-risk rules
- position sizing advisor
- volatility-adjusted sizing
- risk intervention framework
- portfolio charts

## Phase 5: Research AI

### Included

- AI-assisted institutional thesis reports
- bull/base/bear scenario synthesis
- fundamental/factor/valuation context hooks
- plain-English explanations
- user Q&A on ticker research
- missing-data disclosure

## Phase 6: Memory Graph + Discovery Engine

### Included

- user intelligence graph
- facts vs inferences vs hypotheses
- confidence scoring
- hidden edge detection
- hidden risk detection
- behavioral pattern detection
- opportunity detection

## Phase 7: Investment Committee

### Included

- role-specific analysis modules
- analyst vote objects
- synthesis process
- disagreement framework
- risk officer override behavior

## Phase 8: Briefings + Notifications

### Included

- morning briefing
- intraday briefing
- end-of-day review
- weekly review
- text/email/push routing
- alert prioritization

## Phase 9: Wealth Management + Institutional Expansion

### Included

- wealth management AI
- goal-based portfolios
- household/account aggregation architecture
- options intelligence
- volatility analytics
- institutional research workflows
- enterprise users

## Phase 1 Build Contract

The first implementation must add:

1. `praetor_providers.py`
   - provider abstraction
   - OpenAI provider
   - fallback provider
   - timeouts/errors

2. `praetor_context.py`
   - shared context schema
   - page context
   - user context
   - scanner setup context
   - playbook context placeholder

3. `praetor_service.py`
   - Praetor core service
   - scanner question handling
   - trade plan generation
   - playbook summary generation
   - interaction result structure

4. Database schema foundation
   - Praetor interactions
   - memory items
   - playbook rules
   - trade plans

5. API endpoints
   - `POST /api/praetor/ask`
   - `POST /api/praetor/scanner/ask`
   - `POST /api/praetor/trade-plan`
   - `GET /api/praetor/playbook`

6. Scanner UI vertical slice
   - Ask Praetor button
   - Save Trade Plan button
   - response panel in scanner intelligence card

## Success Criteria

Phase 1 is successful if:

- Praetor can answer a scanner-specific question using setup context.
- Praetor can generate a structured trade plan from scanner row data.
- If OpenAI is unavailable, deterministic fallback still returns useful output.
- Interactions can be logged.
- Trade plans can be persisted.
- Future Praetor modules can reuse the same provider/context/service patterns.
