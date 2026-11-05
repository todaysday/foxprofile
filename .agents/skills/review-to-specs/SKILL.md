---
name: review-to-specs
description: Convert code review findings into spec files (epic + subtasks) for module_plan.json. Use when you have a merged review file. Does NOT create OpenSpec change packages - that is orchestrator-workflow job.
---

# Review to Specs

Convert code review findings into spec files for module_plan.json.

**NOT for**: OpenSpec change packages. Use orchestrator-workflow skill for that.

## Input

A merged review file: `openspec/changes/DOC_AUDIT/reviewXX/reviewXX.merged.md`

## Output

- Epic spec: `spec_source/tasks/Z-reviewXX/M10X-ReviewXX-Fixes.md`
- Subtask specs: `spec_source/tasks/Z-reviewXX/M10Xa-description.md`, etc.
- Updated: `ops/module_plan.json`

## Step 0: Read module_plan.json (MANDATORY FIRST)

```bash
# Get existing task IDs to avoid conflicts
cat ops/module_plan.json | jq '.modules[].id'

# Get next available number (e.g., if M108 exists, next is M109)
cat ops/module_plan.json | jq '.modules[].id' | grep -oE 'M[0-9]+' | sort -u | tail -1
```

## Step 1: Analyze Review File

Read and identify:
- Issue severity (Critical/Major/Minor)
- Exact file locations with line numbers
- Dependencies between issues

## Step 2: Group Into Batches

Group related issues by:
1. **Scope**: Same files/packages
2. **Dependency**: Issues that depend on each other
3. **Effort**: Low/Medium/High (3-5 issues per subtask ideal)

### Detail retention (when present)

If the review source includes concrete implementation details (exact steps, guardrails, sequencing, code snippets, migration notes, CI step placement, or edge-case constraints), carry them into subtask specs.

Required behavior (only when such details exist in source):
- Add an `Implementation Notes` section to each affected subtask spec.
- Preserve actionable detail with minimal loss (paraphrase is allowed, dilution is not).
- Keep file references and ordering constraints from the review when they influence correctness.
- Do not collapse detailed guidance into generic one-line “Fix X” statements.

## Step 3: Create Epic Spec

`spec_source/tasks/Z-reviewXX/M10X-ReviewXX-Fixes.md`:

```markdown
# M10X — ReviewXX Fixes

**Track:** Z (reviewXX)
**Type:** epic
**Priority:** low/medium/high

## Summary

Brief description of the overall fix scope.

## Children

| ID | Title | Scope |
|----|-------|-------|
| M10Xa | First batch | Issues X.Y, X.Z |
| M10Xb | Second batch | Issues A.B, A.C |

## Acceptance Criteria

- [ ] All issues from Section X resolved
- [ ] All existing tests pass
```

## Step 4: Create Subtask Specs

`spec_source/tasks/Z-reviewXX/M10Xa-description.md`:

```markdown
# M10Xa — Brief Description

**Track:** Z (reviewXX)
**Type:** subtask
**Priority:** low
**Effort:** low

## Scope

Fix issues:
- X.Y: Issue title
  - Location: `path/to/file.mjs:line`
  - Problem: What is wrong
  - Fix: How to fix it

## Acceptance Criteria

- [ ] Specific fix 1
- [ ] Specific fix 2
- [ ] Tests pass

## Test Commands

```bash
pnpm -C packages/foo test --if-present
```

## Files to Touch

- `path/to/file.mjs`
```

## Step 5: Update module_plan.json (MANDATORY FINAL STEP)

**You MUST update `ops/module_plan.json` with new epic and subtasks.**

### 5.1 Read current structure

```bash
cat ops/module_plan.json | jq '.modules | length'
```

### 5.2 Add epic entry

Append to `.modules` array:

```json
{
  "id": "M10X",
  "deps": [],
  "spec_path": "spec_source/tasks/Z-reviewXX/M10X-ReviewXX-Fixes.md",
  "track": "Z",
  "kind": "epic",
  "children": ["M10Xa", "M10Xb"],
  "exec": false,
  "lane": "core",
  "test_cmd": "pnpm -C packages/foo test && pnpm -C packages/bar test",
  "allowed_paths": [
    "ops/module_plan.json",
    "spec_source/tasks/Z-reviewXX/**",
    "openspec/changes/M10X*/**"
  ],
  "touch_paths": [
    "ops/module_plan.json",
    "spec_source/tasks/Z-reviewXX/**",
    "openspec/changes/M10X*/**",
    "packages/foo/**",
    "packages/bar/**"
  ]
}
```

### 5.3 Add subtask entries

For each subtask, append to `.modules` array:

```json
{
  "id": "M10Xa",
  "deps": [],
  "spec_path": "spec_source/tasks/Z-reviewXX/M10Xa-description.md",
  "track": "Z",
  "kind": "subtask",
  "title": "Brief description",
  "test_cmd": "pnpm -C packages/foo test --if-present",
  "coverage_cmd": "pnpm -C packages/foo run test:coverage --if-present || true",
  "effort_code": "low",
  "effort_tests": "low",
  "lane": "module",
  "allowed_paths": [
    "packages/foo/**",
    "openspec/changes/M10Xa/**"
  ],
  "touch_paths": [
    "packages/foo/**",
    "openspec/changes/M10Xa/**"
  ]
}
```

### 5.4 Use jq to append (example)

```bash
# Create backup first
cp ops/module_plan.json ops/module_plan.json.bak

# Add epic
jq '.modules += [{
  "id": "M10X",
  "deps": [],
  "spec_path": "spec_source/tasks/Z-reviewXX/M10X-ReviewXX-Fixes.md",
  "track": "Z",
  "kind": "epic",
  "children": ["M10Xa", "M10Xb"],
  "exec": false,
  "lane": "core"
}]' ops/module_plan.json > /tmp/plan.json && mv /tmp/plan.json ops/module_plan.json

# Add subtask
jq '.modules += [{
  "id": "M10Xa",
  "deps": [],
  "spec_path": "spec_source/tasks/Z-reviewXX/M10Xa-description.md",
  "track": "Z",
  "kind": "subtask",
  "title": "Brief description",
  "lane": "module",
  "allowed_paths": ["packages/foo/**", "openspec/changes/M10Xa/**"],
  "touch_paths": ["packages/foo/**", "openspec/changes/M10Xa/**"]
}]' ops/module_plan.json > /tmp/plan.json && mv /tmp/plan.json ops/module_plan.json
```

### 5.5 Validate JSON

```bash
jq . ops/module_plan.json > /dev/null && echo "Valid JSON"
```

## What Happens Next

After specs are created AND module_plan.json updated, use **orchestrator-workflow** skill to:
1. Read task config from module_plan.json
2. Create OpenSpec change package
3. Implement the code
4. Create PR and merge

## Checklist Before Finishing

- [ ] Step 0: Read existing task IDs from module_plan.json
- [ ] Step 3: Created epic spec file
- [ ] Step 4: Created all subtask spec files
- [ ] Step 5: Added epic to module_plan.json modules array
- [ ] Step 5: Added all subtasks to module_plan.json modules array
- [ ] Step 5: Set correct allowed_paths for each subtask
- [ ] Step 5: Set correct deps between subtasks if needed
- [ ] Step 5: Validated JSON syntax
- [ ] If review contained implementation details, they were preserved in subtask `Implementation Notes`

## Tips

1. **Check existing IDs**: Read module_plan.json first to avoid ID conflicts
2. **Defer risky changes**: Large refactors → separate task
3. **Validate against code**: Verify line numbers before writing specs
4. **Small batches**: 3-5 issues per subtask
5. **Dependency order**: If B depends on A, put in separate subtasks with deps
6. **Always backup**: Copy module_plan.json before editing
