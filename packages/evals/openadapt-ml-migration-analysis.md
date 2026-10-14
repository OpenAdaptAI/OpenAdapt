# OpenAdapt-ML Migration Analysis Report

## Executive Summary

After reviewing recent PRs and analyzing the openadapt-evals codebase, I've identified several key components that should be migrated to openadapt-ml to better align with the separation of concerns between evaluation infrastructure (openadapt-evals) and ML training/policy runtime (openadapt-ml).

## Recent Changes Analysis

Based on the recent commit history (last month), several significant changes indicate areas ripe for migration:

### Key Recent PRs & Features:

1. **PR #150**: Move open-clip-torch to optional training dependencies - indicates training components are intermixed
2. **PR #148**: Enhanced PlannerGrounderAgent with double_click support and anti-loop recovery
3. **PR #143**: Workflow transcript generation pipeline (Pass 0 + Pass 1)
4. **PR #142**: Workflow extraction Pydantic models, WAA adapter, and matching pipeline
5. **PR #140**: AReaL AgentWorkflow wrapping WAADesktopEnv for RL training

These recent additions show increasing ML/training functionality creeping into the evaluation framework.

## Migration Recommendations

### HIGH PRIORITY - Should Migrate to openadapt-ml

#### 1. Core ML Training Components

**Files to migrate:**
- `openadapt_evals/training/` (entire directory)
  - `areal_workflow.py` - AReaL RL training workflow
  - `trl_rollout.py` - TRL rollout collection
  - `trajectory_logger.py` - Training data logging
  - `planner_cache.py` - Caching for planning agents

**Rationale:** These are pure ML training utilities that don't belong in an evaluation framework. The AReaL workflow especially is a training-specific component.

#### 2. Policy Agent Implementation

**Files to migrate:**
- `openadapt_evals/agents/policy_agent.py`

**Rationale:** This agent loads trained models from openadapt-ml and represents trained policy execution. The current implementation has a circular dependency where openadapt-evals imports from openadapt-ml for model loading. This should be flipped - the policy runtime belongs in openadapt-ml.

**Migration approach:**
1. Move PolicyAgent to openadapt-ml as the canonical policy runtime
2. Create a thin wrapper/adapter in openadapt-evals that imports from openadapt-ml
3. This eliminates the circular dependency and puts policy execution where it belongs

#### 3. Advanced Planning Agents

**Files to migrate:**
- `openadapt_evals/agents/planner_grounder_agent.py`
- Related planning/grounding utilities

**Rationale:** Recent PR #148 added sophisticated ML-based features like anti-loop detection and dialog dismissal awareness. These represent learned behaviors that should be part of the ML pipeline, not evaluation infrastructure.

#### 4. VLM Model Adapters

**Files to migrate:**
- `openadapt_evals/agents/qwen3vl_agent.py`
- Other VLM-specific agent implementations that encapsulate model-specific behavior

**Rationale:** These contain model-specific prompting, parsing, and inference logic that belongs with the ML models themselves.

### MEDIUM PRIORITY - Consider for Migration

#### 1. Annotation Pipeline Components

**Files to consider:**
- `openadapt_evals/annotation.py`
- `openadapt_evals/vlm.py`

**Current status:** The documentation states these were "migrated from `openadapt_ml.experiments.demo_prompt.annotate` so that the eval workflow... does not require a cross-repo dependency on `openadapt-ml` for annotation."

**Recommendation:** Re-evaluate this decision. While it reduces dependencies, annotation is fundamentally an ML task (VLM-based) and might be better placed in openadapt-ml with openadapt-evals importing the functionality.

#### 2. RL Environment Wrapper

**Files to consider:**
- `openadapt_evals/adapters/rl_env.py`
- `openadapt_evals/adapters/verl_env.py` (if exists)

**Rationale:** These provide Gymnasium-style interfaces for RL training. While they wrap evaluation environments, they're specifically designed for training purposes.

**Recommendation:** Create thin adapter interfaces in openadapt-evals, move the substantial RL-specific logic to openadapt-ml.

### LOW PRIORITY - Keep in openadapt-evals

#### 1. Base Agent Interfaces
- `openadapt_evals/agents/base.py`
- `openadapt_evals/agents/api_agent.py` (API-based agents for evaluation)

**Rationale:** These are evaluation interfaces, not ML/training components.

#### 2. Benchmark Adapters
- `openadapt_evals/adapters/` (except RL-specific components)
- WAA adapters, mock adapters, etc.

**Rationale:** These are evaluation infrastructure, not ML components.

#### 3. Infrastructure & VM Management
- `openadapt_evals/infrastructure/`
- All cloud VM, pool management, monitoring components

**Rationale:** Pure evaluation infrastructure.

## Implementation Strategy

### Phase 1: Core Training Components
1. Migrate `openadapt_evals/training/` to `openadapt_ml/training/`
2. Update imports and dependencies
3. Ensure existing evaluation workflows continue to work

### Phase 2: Policy Runtime
1. Move PolicyAgent to openadapt-ml as the canonical policy execution environment
2. Create thin wrapper in openadapt-evals for evaluation compatibility
3. Resolve circular dependency

### Phase 3: Advanced ML Agents
1. Migrate sophisticated ML-based agents (planner_grounder, qwen3vl)
2. Keep simple API-based agents in openadapt-evals
3. Maintain evaluation interfaces

### Phase 4: RL Environment Integration
1. Move substantial RL logic to openadapt-ml
2. Keep thin adapters in openadapt-evals for benchmark integration

## Benefits of Migration

1. **Clear separation of concerns**: Evaluation infrastructure vs ML/training
2. **Reduced circular dependencies**: Cleaner dependency graph
3. **Better modularity**: Teams can work on evaluation vs ML independently
4. **Focused dependencies**: openadapt-evals becomes lighter, openadapt-ml contains ML-heavy dependencies
5. **Improved maintainability**: Related functionality is co-located

## Risks & Considerations

1. **Breaking changes**: Migration will require updates to existing code using these components
2. **Dependency management**: Need to carefully manage the new import structure
3. **CI/CD impact**: Tests and deployment pipelines will need updates
4. **Documentation**: Extensive documentation updates required

## Conclusion

The recent growth in ML/training functionality within openadapt-evals indicates it's time for a strategic refactoring. The suggested migrations will create cleaner architectural boundaries and reduce the evaluation framework's complexity while strengthening openadapt-ml's role as the ML/training hub.

Priority should be given to migrating the training components and resolving the circular dependency with PolicyAgent, as these represent the clearest architectural violations of the intended separation between evaluation and ML concerns.