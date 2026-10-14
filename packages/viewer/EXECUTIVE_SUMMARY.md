# OpenAdaptAI Repository Analysis - Executive Summary
**Date:** January 17, 2026
**Analyst:** Repository Audit
**Total Repositories Analyzed:** 49

---

## TL;DR

**Recommendation:** Archive **23 repositories (47%)** to focus on **24 active/strategic projects (49%)**

**Impact:**
- ✅ Reduce maintenance overhead by 47%
- ✅ Improve contributor clarity
- ✅ Zero breaking changes (archives remain accessible)
- ✅ Clear migration paths for all superseded projects

---

## Current State Snapshot

| Metric | Count | Percentage |
|--------|-------|------------|
| **Total Repositories** | 49 | 100% |
| **Active (< 30 days)** | 13 | 27% |
| **Moderate (1-6 months)** | 4 | 8% |
| **Inactive (> 6 months)** | 30 | 61% |
| **Already Archived** | 2 | 4% |

---

## Target State (Post-Cleanup)

| Category | Count | Percentage |
|----------|-------|------------|
| **Active Core** | 13 | 50% |
| **Strategic Keep** | 11 | 42% |
| **Archived** | 25 | 48% |
| **Total Active** | 24 | 52% |

---

## The 13 Active Core Repositories (KEEP)

These repositories have commits in the last 30 days and form OpenAdapt's current architecture:

1. **OpenAdapt** (1,470 ⭐) - Main repository
2. **openadapt-web** (7 ⭐) - Official website
3. **openadapt-tray** - System tray GUI (NEW)
4. **openadapt-evals** - Evaluation framework (NEW)
5. **openadapt-viewer** - HTML visualization components (NEW)
6. **openadapt-ml** (2 ⭐) - ML training toolkit
7. **openadapt-capture** - Data collection infrastructure
8. **openadapt-retrieval** - Multimodal demo retrieval (NEW)
9. **openadapt-grounding** - UI element detection (NEW)
10. **openadapt-agent** - Production execution engine (NEW)
11. **openadapt-telemetry** - Error tracking (NEW)
12. **openadapt-privacy** - PII/PHI protection
13. **.github** - Org configuration

**Status:** All 13 are part of the new modular architecture. KEEP ALL.

---

## 11 Strategic Repositories (KEEP - Lower Activity)

| Repository | Stars | Last Commit | Rationale |
|------------|-------|-------------|-----------|
| **OmniMCP** | 68 | 2025-04-08 | Popular community project |
| **PydanticPrompt** | 5 | 2025-04-06 | Useful utility library |
| **OpenAdapter** | 1 | 2025-02-18 | Cloud deployment infrastructure |
| **OmniParser** | 4 | 2025-02-14 | Fork with OpenAdapt modifications |
| **OpenCUA** | 1 | 2025-08-18 | Research dataset reference |
| **SoM** | 13 | 2024-06-05 | Set-of-Mark prompting (fork) |
| **atomacos** | 7 | 2023-08-08 | **Critical:** macOS automation dependency |
| **pynput** | 4 | 2023-08-08 | **Critical:** Input control dependency |
| **app** | 0 | 2025-01-11 | **REVIEW:** Empty repo, decide in 30 days |
| **openadapt-new** | 0 | - | Already archived (correct) |
| **OpenAdaptVault** | 2 | - | Already archived (correct) |

---

## 23 Repositories to Archive (IMMEDIATE ACTION)

### Category A: Research Forks (17 repos - Archive in Phase 1)

These are forks of external research projects with zero modifications and minimal engagement:

1. whisper
2. ultralytics
3. grok-1
4. Self-Rewarding-Language-Models
5. Prompt-Engineering-Guide
6. prismer
7. active-prompt
8. samapi
9. R1-V
10. Qwen2.5-VL
11. open-r1-multimodal
12. Janus
13. CogVLM
14. Awesome-LLM-Reasoning
15. llama-agentic-system
16. omniparser-api
17. OmniMCP.web

**Action:** Archive immediately with redirect messages to upstream sources.

### Category B: Superseded/Obsolete (5 repos - Archive in Phase 2)

| Repository | Replacement | Migration Required |
|------------|-------------|-------------------|
| **OpenSanitizer** | openadapt-privacy | YES - migration guide needed |
| **OpenReflector** | N/A (experimental) | NO - minimal usage |
| **procdoc** | N/A (inactive) | NO - abandoned |
| **openadapt-gitbook** | openadapt.ai + READMEs | NO - docs migrated |
| **oa.blog** | openadapt.ai | NO - inactive |

**Action:** 30-day deprecation notice, then archive.

### Category C: Under Review (1 repo - Decide in Phase 4)

| Repository | Issue | Options |
|------------|-------|---------|
| **UI-TARS-desktop** | Fork with minimal changes | Archive OR Keep if strategic value identified |

---

## Critical Dependencies (DO NOT ARCHIVE)

These repositories have active dependencies:

| Repository | Used By | Why Keep |
|------------|---------|----------|
| **atomacos** | OpenAdapt (macOS) | macOS automation critical dependency |
| **pynput** | OpenAdapt | Input control critical dependency |
| **openadapt-privacy** | openadapt-capture, openadapt-ml | PII/PHI protection |
| **openadapt-viewer** | openadapt-evals, openadapt-ml | Visualization infrastructure |
| **openadapt-capture** | openadapt-ml, openadapt-evals | Data collection |
| **openadapt-grounding** | openadapt-agent | UI element detection |

---

## Financial Impact

### Current Maintenance Cost (Estimated)

- 49 repositories × 30 min/month average = **24.5 hours/month**
- Research forks with no activity = **8.5 hours/month wasted**
- Obsolete projects with occasional issues = **2 hours/month wasted**

### Post-Cleanup Maintenance Cost

- 24 repositories × 30 min/month average = **12 hours/month**
- **Savings: 12.5 hours/month (51% reduction)**

### One-Time Cleanup Cost

- Phase 1 archival: **3 hours**
- Phase 2 deprecation notices: **4 hours**
- Phase 2 migration guides: **8 hours**
- Phase 3 final archival: **2 hours**
- Phase 4 strategic review: **4 hours**

**Total:** ~21 hours one-time investment for 51% ongoing savings

---

## Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Breaking external dependencies | Medium | Low | Archives remain accessible; deprecation period |
| Lost documentation | Medium | Low | Audit before archival; migrate critical docs |
| Community backlash | Low | Low | Clear communication; migration guides |
| Accidentally archive active project | High | Very Low | Multi-person review; dependency checking |

**Overall Risk:** LOW - Well-mitigated with phased approach

---

## Phased Implementation Timeline

### Phase 1: Week 1-2 (Low Risk)
- **Action:** Archive 17 research forks
- **Time:** 3 hours
- **Risk:** Minimal

### Phase 2: Week 3-4 (Deprecation Notices)
- **Action:** Post deprecation notices for 5 repos
- **Time:** 4 hours
- **Risk:** Low

### Phase 3: Month 2 (Final Archival)
- **Action:** Archive Phase 2 repos after 30-day notice
- **Time:** 2 hours
- **Risk:** Low

### Phase 4: Month 3 (Strategic Decisions)
- **Action:** Decide on app, UI-TARS-desktop, fork refreshes
- **Time:** 4 hours
- **Risk:** Medium

**Total Timeline:** 3 months
**Total Effort:** ~21 hours

---

## Key Decisions Required

### Decision 1: app Repository (URGENT)
**Issue:** Created Jan 11, 2025. Only LICENSE file, no code.

**Options:**
- A. Archive immediately (no value)
- B. 30-day development deadline
- C. Transfer to developer

**Recommendation:** B - 30-day deadline

**Rationale:** Recent creation suggests intent, but similar functionality exists in openadapt-tray.

### Decision 2: Fork Maintenance Strategy
**Issue:** atomacos and pynput are 18+ months stale but critical dependencies.

**Options:**
- A. Keep as-is
- B. Sync with upstream
- C. Contribute changes upstream

**Recommendation:** B - Sync with upstream quarterly

**Rationale:** Critical dependencies requiring maintenance.

### Decision 3: Community Projects
**Issue:** OmniMCP (68 stars) has 9 months since last commit.

**Options:**
- A. Archive (inactive)
- B. Keep (community value)
- C. Transfer to community maintainer

**Recommendation:** B - Keep

**Rationale:** Highest community engagement outside main repo.

---

## Success Metrics

### Quantitative Metrics
- ✅ **Reduce repository count:** 49 → 24 (51% reduction)
- ✅ **Increase active percentage:** 27% → 50%
- ✅ **Reduce maintenance time:** 51% reduction
- ✅ **Zero breaking changes:** All archives remain accessible

### Qualitative Metrics
- ✅ **Improved contributor clarity:** Clear which repos are active
- ✅ **Better discoverability:** Active projects easier to find
- ✅ **Reduced confusion:** Obsolete projects clearly marked
- ✅ **Professional appearance:** Focused, maintained portfolio

---

## Communication Plan

### Internal (Week 1)
1. Present analysis to core team
2. 1-week review period for feedback
3. Get sign-off from project leads

### External (Phased)
1. **Week 1:** Blog post announcing consolidation
2. **Week 3:** Post deprecation notices with migration guides
3. **Month 2:** Archive Phase 2 with final notices
4. **Month 3:** Success summary and governance policy

---

## Recommended Immediate Actions

### This Week:
1. ✅ Review this analysis
2. ✅ Approve or modify archival plan
3. ✅ Set Phase 1 start date
4. ✅ Assign responsibility for execution

### Week 1-2 (Phase 1):
5. Archive 17 research forks
6. Update documentation
7. Monitor for issues

### Week 3-4 (Phase 2 Start):
8. Post deprecation notices
9. Create migration guides
10. Publish blog post

---

## Questions for Discussion

1. **app repository:** Archive now or give 30-day deadline?
2. **UI-TARS-desktop:** Archive or identify strategic value?
3. **atomacos/pynput:** Sync with upstream or keep as-is?
4. **Execution timeline:** Start Phase 1 this week or next?
5. **Communication:** Who owns blog post and external communication?

---

## Conclusion

OpenAdaptAI has evolved from a monolithic repository to a modular ecosystem with 13 active core packages. The organization now contains 49 repositories, with 61% inactive for over 6 months.

**This cleanup will:**
- Focus effort on 24 active/strategic repositories
- Archive 23 inactive/obsolete projects (read-only preservation)
- Reduce maintenance overhead by 51%
- Improve clarity for contributors and users
- Establish governance for future repository management

**Recommendation:** Proceed with phased archival plan.

**Risk:** LOW - Archives remain accessible, deprecation periods provided, migration guides created.

**Timeline:** 3 months total, 21 hours effort

**ROI:** 51% ongoing maintenance reduction for 21-hour investment

---

## Appendix: Full Documentation

This executive summary is part of a comprehensive analysis package:

1. **EXECUTIVE_SUMMARY.md** (this file) - Quick reference and decisions
2. **openadaptai_repository_analysis.md** - Detailed analysis with rationale
3. **openadaptai_repository_inventory.csv** - Full repository spreadsheet
4. **archival_action_plan.md** - Step-by-step execution guide

**Files Location:** `/path/to/openadapt-viewer/`

---

## Approval

- [ ] Analysis reviewed by: _______________
- [ ] Plan approved by: _______________
- [ ] Start date: _______________
- [ ] Assigned to: _______________

**Signature:** _______________ **Date:** _______________

---

*For questions or clarification, please review the detailed analysis document or contact the project team.*
