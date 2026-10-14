# OpenAdaptAI Repository Analysis & Archival Recommendations
**Analysis Date:** January 17, 2026
**Total Repositories Analyzed:** 49

---

## Executive Summary

The OpenAdaptAI organization has undergone significant architectural evolution, transitioning from a monolithic OpenAdapt repository to a modular ecosystem of specialized packages. This analysis identifies repositories that should be archived, consolidated, or maintained based on activity, strategic value, and dependencies.

**Key Findings:**
- **Active Core:** 13 repositories actively maintained (last commit < 30 days)
- **Candidates for Archive:** 23 repositories (inactive >6 months or obsolete forks)
- **Already Archived:** 2 repositories
- **Strategic Keep:** 11 repositories (low activity but strategic value)

---

## Repository Status Matrix

### TIER 1: ACTIVE CORE REPOSITORIES (KEEP)
These repositories form the active foundation of OpenAdaptAI's current architecture.

| Repository | Stars | Forks | Last Commit | Open Issues | Status | Strategic Value |
|------------|-------|-------|-------------|-------------|---------|-----------------|
| **OpenAdapt** | 1,470 | 216 | 2026-01-17 | 0 | Active | CRITICAL - Main repo, high visibility |
| **openadapt-web** | 7 | 12 | 2026-01-17 | 5 | Active | HIGH - Official website |
| **openadapt-tray** | 0 | 0 | 2026-01-17 | 1 | Active | HIGH - New GUI interface |
| **openadapt-evals** | 0 | 0 | 2026-01-17 | 0 | Active | HIGH - Benchmark framework |
| **openadapt-viewer** | 0 | 0 | 2026-01-17 | 0 | Active | HIGH - Shared visualization |
| **openadapt-ml** | 2 | 0 | 2026-01-17 | 1 | Active | HIGH - ML training toolkit |
| **openadapt-capture** | 0 | 0 | 2026-01-17 | 0 | Active | HIGH - Data collection |
| **openadapt-retrieval** | 0 | 0 | 2026-01-17 | 0 | Active | MEDIUM - Demo retrieval |
| **openadapt-grounding** | 0 | 0 | 2026-01-17 | 0 | Active | MEDIUM - UI element detection |
| **openadapt-agent** | 0 | 0 | 2026-01-17 | 0 | Active | HIGH - Production execution |
| **openadapt-telemetry** | 0 | 0 | 2026-01-17 | 0 | Active | MEDIUM - Error tracking |
| **openadapt-privacy** | 0 | 2 | 2026-01-17 | 0 | Active | HIGH - PII/PHI protection |
| **.github** | 0 | 0 | 2026-01-17 | 0 | Active | LOW - Org settings |

**Recommendation:** KEEP ALL - These form the active modular architecture.

---

### TIER 2: STRATEGIC REPOS WITH MODERATE ACTIVITY (KEEP)

| Repository | Stars | Forks | Last Commit | Open Issues | Status | Strategic Value |
|------------|-------|-------|-------------|-------------|---------|-----------------|
| **OmniMCP** | 68 | 13 | 2025-04-08 | 12 | Moderate | HIGH - Popular integration |
| **PydanticPrompt** | 5 | 0 | 2025-04-06 | 0 | Moderate | MEDIUM - Utility library |
| **OpenAdapter** | 1 | 0 | 2025-02-18 | 3 | Low | MEDIUM - Cloud deployment |
| **OmniParser** | 4 | 1 | 2025-02-14 | 1 | Low | MEDIUM - Fork with changes |
| **app** | 0 | 3 | 2025-01-11 | 0 | Stale | HIGH - Desktop app (planned) |
| **OpenCUA** | 1 | 0 | 2025-08-18 | 0 | Fork | MEDIUM - Research dataset |
| **SoM** | 13 | 5 | 2024-06-05 | 1 | Low | MEDIUM - Fork with changes |
| **atomacos** | 7 | 1 | 2023-08-08 | 0 | Stale | HIGH - macOS automation |
| **pynput** | 4 | 1 | 2023-08-08 | 0 | Stale | HIGH - Input control |

**Recommendation:** KEEP - Strategic dependencies or community value despite lower activity.

**Special Note on 'app':** This repository has only a LICENSE file and no code. Should either be developed or archived within 3 months.

---

### TIER 3: RECENTLY ARCHIVED (CORRECT DECISION)

| Repository | Stars | Forks | Last Commit | Status | Rationale |
|------------|-------|-------|-------------|---------|-----------|
| **openadapt-new** | 0 | 0 | 2026-01-17 | Archived | Sandbox/experimental |
| **OpenAdaptVault** | 2 | 0 | 2024-10-30 | Archived | Archival snapshot |

**Recommendation:** Already archived - correct decisions.

---

### TIER 4: CANDIDATES FOR IMMEDIATE ARCHIVAL

#### 4A. OBSOLETE/SUPERSEDED PROJECTS

| Repository | Stars | Forks | Last Commit | Reason for Archive | Archive Message |
|------------|-------|-------|-------------|-------------------|-----------------|
| **OpenSanitizer** | 5 | 0 | 2024-10-31 | Superseded by openadapt-privacy | "This project has been superseded by openadapt-privacy which provides enhanced PII/PHI detection. Please migrate to: https://github.com/OpenAdaptAI/openadapt-privacy" |
| **OpenReflector** | 2 | 0 | 2024-10-31 | Experimental, minimal adoption | "Experimental project for Anthropic Computer Use integration. Archived due to limited adoption and integration into main OpenAdapt workflow." |
| **procdoc** | 0 | 0 | 2024-08-08 | Inactive >6 months, no README | "Process documentation experiment archived due to inactivity. Functionality may be integrated into future OpenAdapt releases." |
| **openadapt-gitbook** | 1 | 1 | 2023-12-13 | Documentation moved | "Documentation has been migrated to main website and repo READMEs. See https://openadapt.ai for current documentation." |
| **oa.blog** | 0 | 1 | 2023-07-27 | Inactive blog fork | "Blog theme fork archived. OpenAdapt news and updates available at https://openadapt.ai" |

#### 4B. INACTIVE RESEARCH FORKS (>6 months, minimal changes)

| Repository | Stars | Forks | Last Commit | Original Source | Archive Message |
|------------|-------|-------|-------------|-----------------|-----------------|
| **OmniMCP.web** | 0 | 1 | 2025-03-22 | Netlify template fork | "Web interface fork for OmniMCP. Archived in favor of direct implementation. See OmniMCP repository for current development." |
| **omniparser-api** | 0 | 0 | 2024-11-26 | Community fork | "Self-hosted OmniParser fork archived. Use official Microsoft OmniParser or OpenAdaptAI/OmniParser for maintained implementations." |
| **llama-agentic-system** | 1 | 0 | 2024-07-23 | Meta/llama-agentic-system | "Research fork archived. Refer to upstream Meta repository for active development." |
| **whisper** | 0 | 0 | 2024-07-01 | OpenAI/whisper | "Fork archived. Use official OpenAI Whisper repository: https://github.com/openai/whisper" |
| **ultralytics** | 0 | 0 | 2024-05-30 | ultralytics/ultralytics | "Fork archived. Use official Ultralytics YOLOv8: https://github.com/ultralytics/ultralytics" |
| **grok-1** | 0 | 0 | 2024-03-18 | xai-org/grok-1 | "Fork archived. Refer to xAI's official repository for Grok model." |
| **Self-Rewarding-Language-Models** | 0 | 0 | 2024-03-15 | lucidrains/self-rewarding-lm | "Research fork archived. See upstream repository for implementation." |
| **Prompt-Engineering-Guide** | 0 | 1 | 2024-03-10 | dair-ai/Prompt-Engineering-Guide | "Fork archived. Use official DAIR.AI guide: https://github.com/dair-ai/Prompt-Engineering-Guide" |
| **prismer** | 1 | 0 | 2024-01-17 | NVlabs/prismer | "Research fork archived. Refer to NVIDIA's original repository." |
| **active-prompt** | 0 | 0 | 2023-11-22 | shreyashankar/active-prompt | "Research fork archived. See original repository for implementation." |
| **samapi** | 0 | 0 | 2023-09-15 | whateverusename/samapi | "Segment Anything API fork archived. Use official Meta SAM repository." |

#### 4C. STALE RESEARCH FORKS (Minimal stars, zero engagement)

| Repository | Stars | Forks | Last Commit | Original Source | Archive Message |
|------------|-------|-------|-------------|-----------------|-----------------|
| **R1-V** | 0 | 0 | 2025-02-05 | R1-V VLM research | "Research reference fork archived. Refer to original source for active development." |
| **Qwen2.5-VL** | 0 | 0 | 2025-01-28 | QwenLM/Qwen2.5-VL | "Fork archived. Use official Qwen repository: https://github.com/QwenLM/Qwen2.5-VL" |
| **open-r1-multimodal** | 0 | 0 | 2025-01-29 | Research fork | "Multimodal training fork archived. Refer to open-r1 upstream repository." |
| **Janus** | 0 | 0 | 2025-01-27 | deepseek-ai/Janus | "Research fork archived. Use official DeepSeek Janus repository." |
| **UI-TARS-desktop** | 1 | 1 | 2025-01-21 | UI-TARS fork | "Desktop application fork archived. Limited adoption and maintenance overhead." |
| **CogVLM** | 2 | 1 | 2024-03-07 | THUDM/CogVLM | "Fork archived. Use official Tsinghua CogVLM: https://github.com/THUDM/CogVLM" |
| **Awesome-LLM-Reasoning** | 3 | 1 | 2024-03-08 | atfortes/LLM-Reasoning-Papers | "Resource fork archived. Refer to original curated list for updates." |

**Total Candidates for Archival:** 23 repositories

**Recommendation:** Archive all repositories in Tier 4 with appropriate messages directing users to either:
1. The superseding OpenAdapt repository
2. The original upstream source
3. Current OpenAdapt documentation

---

## Dependency Analysis

### Critical Dependencies (Must Keep)

**For Main OpenAdapt Repository:**
- **atomacos** - macOS automation (forked with modifications)
- **pynput** - Input control (forked with modifications)
- **SoM** - Set-of-Mark prompting (forked with modifications)
- **OmniParser** - UI parsing (forked with modifications)

**For New Modular Architecture:**
- **openadapt-privacy** - Used by openadapt-capture, openadapt-ml
- **openadapt-viewer** - Used by openadapt-evals, openadapt-ml
- **openadapt-capture** - Used by openadapt-ml, openadapt-evals
- **openadapt-grounding** - Used by openadapt-agent
- **openadapt-retrieval** - Used by openadapt-ml

### External Projects with Value

- **OmniMCP** (68 stars) - Popular integration, active community
- **PydanticPrompt** (5 stars) - Useful utility library
- **OpenCUA** - Research dataset reference

### Superseded Projects

| Old Repository | Superseded By | Migration Path |
|----------------|---------------|----------------|
| OpenSanitizer | openadapt-privacy | Direct replacement |
| openadapt-gitbook | READMEs + website | Documentation consolidated |

---

## Migration Plan for Dependencies

### Phase 1: Immediate (Week 1-2)
1. Archive all research forks in Tier 4B and 4C (17 repos)
2. Update any internal documentation referencing these repos
3. Add clear archive messages with redirects

### Phase 2: Deprecation Period (Month 1)
1. Add deprecation notices to OpenSanitizer README
2. Create migration guide for OpenSanitizer → openadapt-privacy
3. Add deprecation notices to OpenReflector, procdoc
4. Monitor for any unexpected dependencies

### Phase 3: Final Archival (Month 2)
1. Archive OpenSanitizer, OpenReflector, procdoc
2. Archive OmniMCP.web, omniparser-api
3. Archive documentation repos (openadapt-gitbook, oa.blog)

### Phase 4: Strategic Review (Month 3)
1. Evaluate 'app' repository - develop or archive
2. Review low-engagement forks (UI-TARS-desktop)
3. Assess continued maintenance of atomacos, pynput forks

---

## Recommended Archive Messages

### Template A: Superseded by OpenAdapt Package

```
🔒 ARCHIVED

This repository has been superseded by [new-package-name] as part of OpenAdapt's
modular architecture.

**Migration:** Please use https://github.com/OpenAdaptAI/[new-package]
**Documentation:** https://openadapt.ai

This archive is preserved for historical reference.
```

### Template B: Upstream Research Fork

```
🔒 ARCHIVED

This research fork has been archived. Please refer to the original repository
for active development and updates:

**Upstream:** [original-repo-url]

This archive served as a reference during OpenAdapt development but is no
longer maintained.
```

### Template C: Inactive/Experimental

```
🔒 ARCHIVED

This experimental project has been archived due to [reason: limited adoption/
superseded functionality/integrated into main repo].

**Active Development:** https://github.com/OpenAdaptAI/OpenAdapt
**Documentation:** https://openadapt.ai

Preserved for historical reference.
```

---

## Special Considerations

### Repositories Requiring Discussion

1. **app** (0 stars, 3 forks, empty)
   - **Issue:** Only LICENSE file, no code
   - **Options:**
     - Archive now (no code to maintain)
     - Set 90-day deadline for initial commit
     - Transfer to experimental/incubator org
   - **Recommendation:** Archive unless development starts within 30 days

2. **atomacos** (7 stars, forked)
   - **Issue:** 18 months since last commit
   - **Value:** macOS automation dependency
   - **Options:**
     - Keep as fork (critical dependency)
     - Merge changes upstream
     - Archive and use upstream
   - **Recommendation:** Keep - critical macOS dependency

3. **pynput** (4 stars, forked)
   - **Issue:** 17 months since last commit
   - **Value:** Input control dependency
   - **Options:**
     - Keep as fork (critical dependency)
     - Merge changes upstream
     - Archive and use upstream
   - **Recommendation:** Keep - critical input control dependency

4. **OmniMCP** (68 stars, active community)
   - **Status:** Moderate activity (last commit 9 months ago)
   - **Value:** Highest community engagement outside main repo
   - **Recommendation:** Keep - active community, strategic value

---

## Summary Statistics

### Current State
- **Total Repositories:** 49
- **Already Archived:** 2 (4%)
- **Active (< 30 days):** 13 (27%)
- **Moderate Activity (1-6 months):** 4 (8%)
- **Inactive (> 6 months):** 30 (61%)

### Recommended Actions
- **Keep Active:** 13 repositories
- **Keep Strategic:** 11 repositories
- **Archive Immediately:** 17 repositories (research forks)
- **Archive After Deprecation:** 6 repositories (superseded/obsolete)
- **Under Review:** 2 repositories (app, UI-TARS-desktop)

### Expected Post-Cleanup State
- **Active Repositories:** 24 (49%)
- **Archived Repositories:** 25 (51%)

This would reduce maintenance burden while preserving all strategic and active projects.

---

## Implementation Timeline

### Week 1-2: Quick Wins (17 repos)
Archive inactive research forks with Template B messages.

**Repositories:**
- whisper, ultralytics, grok-1, Self-Rewarding-Language-Models
- Prompt-Engineering-Guide, prismer, active-prompt, samapi
- R1-V, Qwen2.5-VL, open-r1-multimodal, Janus
- CogVLM, Awesome-LLM-Reasoning, llama-agentic-system
- omniparser-api, OmniMCP.web

**Impact:** Minimal risk - all are research forks with no dependencies

### Week 3-4: Documentation Cleanup (2 repos)
Archive documentation repositories after verifying migration.

**Repositories:**
- openadapt-gitbook
- oa.blog

**Prerequisites:**
- Confirm docs migrated to openadapt.ai
- Update any external links

### Month 2: Superseded Projects (3 repos)
Archive after 30-day deprecation notice.

**Repositories:**
- OpenSanitizer → openadapt-privacy
- OpenReflector (experimental)
- procdoc (inactive)

**Prerequisites:**
- Post deprecation notices
- Create migration guides
- Monitor for usage

### Month 3: Strategic Decision (3 repos)
Evaluate and decide on edge cases.

**Repositories:**
- app (develop or archive)
- UI-TARS-desktop (assess value)
- Additional low-engagement forks

---

## Archival Checklist

For each repository being archived:

- [ ] Verify no active dependencies in other OpenAdapt repos
- [ ] Check for any open PRs or issues requiring response
- [ ] Add comprehensive archive message to README
- [ ] Update organization-level documentation
- [ ] Set GitHub archive status
- [ ] Add redirect in .github repo if needed
- [ ] Update openadapt.ai documentation
- [ ] Notify any known users/contributors
- [ ] Update package registry metadata (PyPI, npm)
- [ ] Archive any related project boards or wikis

---

## Conclusion

OpenAdaptAI's repository portfolio reflects natural growth and experimentation, but now requires consolidation. The transition to a modular architecture with focused packages (openadapt-ml, openadapt-capture, etc.) represents the strategic direction.

**Key Recommendations:**
1. Archive 23 repositories immediately (research forks, obsolete projects)
2. Maintain 24 active/strategic repositories
3. Create clear migration paths for superseded projects
4. Establish policy for future fork management
5. Review app repository within 30 days

This cleanup will:
- Reduce maintenance overhead
- Clarify project structure for new contributors
- Improve discoverability of active projects
- Preserve historical reference while focusing effort

**Next Steps:**
1. Review this analysis with core team
2. Begin Phase 1 archival (research forks)
3. Set up deprecation notices for Phase 2
4. Make strategic decisions on edge cases (app, UI-TARS-desktop)
5. Document archival policy for future forks

---

**Analyst Notes:**
- All data current as of January 17, 2026
- Repository activity measured by last commit date
- Community engagement measured by stars/forks/issues
- Strategic value assessed based on dependencies and roadmap alignment
