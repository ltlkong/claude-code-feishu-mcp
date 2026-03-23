---
name: dependency-awareness
description: Ensures full dependency mapping before any multi-file or multi-step task. Use this skill whenever working with multiple files, tracing data flow, handling chained conversations, or making changes that could ripple across the codebase. Trigger when the user asks to modify, refactor, debug, or build anything involving more than one file, data source, or conversation thread — even if they don't mention "dependencies" explicitly.
---

# Dependency Awareness

Before touching anything, understand what connects to what. Changes without dependency awareness break things silently. This skill exists because a single edit can cascade through imports, data pipelines, and conversation context in ways that aren't obvious at first glance.

## When This Kicks In

- Editing or creating multiple files
- Debugging across modules
- Refactoring shared code
- Tracing where data comes from or goes
- Acting on requests that reference earlier conversation context
- Any task where "what else does this affect?" is a valid question

## The Three Dependency Layers

### 1. File Dependencies

Before modifying any file, answer: **what imports this, and what does this import?**

**Process:**
1. Read the target file first
2. Identify all imports/requires/includes — trace them to their source files
3. Search for reverse dependencies — what other files import the target
4. Map the dependency chain: `A imports B imports C` means changing C affects both B and A
5. Check for circular dependencies or shared state (globals, singletons, shared configs)

**What to look for:**
- `import` / `from X import` / `require()` / `include` statements
- Shared config files (`.env`, `config.py`, `settings.json`)
- Shared types, interfaces, or base classes
- Event emitters/listeners that couple files indirectly
- Database models referenced across multiple modules

**Before editing, state the dependency map.** Even a one-liner like "This file is imported by X and Y, and imports Z" is enough. The point is to prove you looked.

### 2. Conversation Dependencies

Messages don't exist in isolation. The user's current request often depends on prior context — decisions made, files discussed, constraints established earlier.

**Process:**
1. Check if the current request references something from earlier ("the thing we discussed", "that file", "like before")
2. Identify any decisions or constraints established in prior messages that still apply
3. If the user's request contradicts an earlier decision, flag it — don't silently override
4. Track evolving requirements: what the user said first vs. what they refined later — the latest version wins, but acknowledge the change

**What breaks without this:**
- Implementing something the user already rejected three messages ago
- Ignoring constraints they set earlier ("keep it under 100 lines", "don't change the API")
- Losing context about WHY a decision was made, not just WHAT was decided

### 3. Data Dependencies

Data flows through systems — APIs, databases, transforms, caches, message queues. Changing one node in the pipeline affects downstream consumers.

**Process:**
1. Identify the data source (API endpoint, database table, file, user input)
2. Trace the transformation chain: raw data -> processing -> output
3. Find all consumers of the data (who reads it, who displays it, who exports it)
4. Check data format assumptions — if upstream changes shape, downstream breaks
5. Verify data integrity: types, required fields, validation rules

**What to look for:**
- API response shapes that multiple components depend on
- Database schema changes that affect queries elsewhere
- Shared data models or DTOs passed between modules
- Cache invalidation — if the source changes, is the cache still valid?
- Environment-specific data sources (dev vs. prod)

## How to Apply This

**For small changes (1-2 files):** Quick mental check — state the dependencies in one sentence before proceeding.

**For medium changes (3-5 files):** List the dependency chain explicitly. Identify which files are "upstream" (providers) vs. "downstream" (consumers).

**For large changes (6+ files or cross-cutting):** Build a dependency map before writing any code. Group files by their role in the dependency graph. Identify the safest edit order — start with leaf nodes (no dependents), work toward root nodes (many dependents).

## Edit Order Matters

When multiple files need changes, order them to minimize breakage:

1. **Shared types/interfaces first** — everything else depends on these
2. **Utility/helper modules** — used by many, depend on few
3. **Core logic/services** — the meat of the change
4. **API/routes/handlers** — the entry points
5. **Tests last** — they verify everything above

If you change a downstream file before its upstream dependency, you're working with stale assumptions.

## Red Flags

Stop and re-examine if you notice:
- A file has no imports but is changing behavior — something is coupling indirectly (events, globals, monkey-patching)
- The user's request doesn't match the data flow you've traced — clarify before building
- A "simple" change touches more than 3 files — the dependency graph is telling you something
- You're about to duplicate logic that already exists in a dependency — reuse it instead
