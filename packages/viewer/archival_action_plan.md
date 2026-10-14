# OpenAdaptAI Repository Archival - Action Plan
**Date:** January 17, 2026
**Purpose:** Systematic cleanup of inactive and obsolete repositories

---

## Quick Reference

**Total Repositories:** 49
**Recommend Archive:** 23 (47%)
**Recommend Keep:** 24 (49%)
**Already Archived:** 2 (4%)

---

## PHASE 1: Immediate Archival (Week 1-2)
**Target:** 17 research forks with zero engagement
**Risk:** Minimal - no dependencies, no community activity
**Time:** 2-3 hours total

### Research Forks to Archive

1. **whisper** - OpenAI Whisper fork
2. **ultralytics** - YOLOv8 fork
3. **grok-1** - Grok model reference
4. **Self-Rewarding-Language-Models** - Research fork
5. **Prompt-Engineering-Guide** - Resource collection
6. **prismer** - NVIDIA research fork
7. **active-prompt** - Research implementation
8. **samapi** - SAM API fork
9. **R1-V** - VLM research reference
10. **Qwen2.5-VL** - Qwen model fork
11. **open-r1-multimodal** - Training fork
12. **Janus** - DeepSeek model fork
13. **CogVLM** - Tsinghua research fork
14. **Awesome-LLM-Reasoning** - Paper collection
15. **llama-agentic-system** - Meta Llama fork
16. **omniparser-api** - API wrapper fork
17. **OmniMCP.web** - Netlify template

### Action Checklist (per repository)

```bash
# For each repository:
1. [ ] Verify zero dependencies in OpenAdaptAI org
2. [ ] Check for any open issues/PRs requiring response
3. [ ] Add archive notice to README
4. [ ] Archive repository via GitHub settings
5. [ ] Update internal documentation if referenced
```

### Archive Notice Template

```markdown
# 🔒 ARCHIVED REPOSITORY

This fork has been archived as it was created for reference during OpenAdapt
development but is no longer actively maintained.

**Active Development:** [link to upstream repository]

For OpenAdapt-related development, see:
- Main Repository: https://github.com/OpenAdaptAI/OpenAdapt
- Documentation: https://openadapt.ai

---
*Archived on [DATE] as part of repository consolidation.*
```

### Automation Script

```bash
#!/bin/bash
# Archive Phase 1 repositories

REPOS=(
  "whisper"
  "ultralytics"
  "grok-1"
  "Self-Rewarding-Language-Models"
  "Prompt-Engineering-Guide"
  "prismer"
  "active-prompt"
  "samapi"
  "R1-V"
  "Qwen2.5-VL"
  "open-r1-multimodal"
  "Janus"
  "CogVLM"
  "Awesome-LLM-Reasoning"
  "llama-agentic-system"
  "omniparser-api"
  "OmniMCP.web"
)

for repo in "${REPOS[@]}"; do
  echo "Processing: $repo"

  # Add archive notice to README
  gh repo edit "OpenAdaptAI/$repo" --description "🔒 ARCHIVED - See README for alternatives"

  # Archive the repository (requires admin permissions)
  gh repo archive "OpenAdaptAI/$repo" --yes

  echo "✓ Archived: $repo"
done
```

---

## PHASE 2: Deprecation Notice (Week 3-4)
**Target:** 5 superseded/obsolete projects
**Risk:** Low - functionality replaced or moved
**Time:** 1 week for notices, 30-day deprecation period

### Repositories with Deprecation Period

| Repository | Replacement | Users to Notify |
|------------|-------------|-----------------|
| **OpenSanitizer** | openadapt-privacy | Check PyPI downloads |
| **OpenReflector** | N/A (experimental) | Check stars/forks |
| **procdoc** | N/A (inactive) | Minimal |
| **openadapt-gitbook** | openadapt.ai + READMEs | Check external links |
| **oa.blog** | openadapt.ai | Minimal |

### Step 1: Add Deprecation Notice (Week 3)

**Notice Template for README:**

```markdown
# ⚠️ DEPRECATION NOTICE

**This repository will be archived on [DATE].**

## Reason
[Choose appropriate reason:]
- This functionality has been moved to [new location]
- This project has been superseded by [new project]
- This documentation has been consolidated at [new location]
- This experimental project is no longer maintained

## Migration Path
[Provide specific migration instructions]

## Timeline
- **Deprecation Notice:** [DATE]
- **Archive Date:** [DATE + 30 days]

For questions or concerns, please open an issue before [DATE].
```

### Step 2: Monitor for Feedback (Week 4)

```bash
# Check for new issues/comments
gh issue list --repo OpenAdaptAI/OpenSanitizer
gh issue list --repo OpenAdaptAI/OpenReflector
gh issue list --repo OpenAdaptAI/procdoc
gh issue list --repo OpenAdaptAI/openadapt-gitbook
gh issue list --repo OpenAdaptAI/oa.blog

# Check for package usage
# For OpenSanitizer specifically:
curl https://pypistats.org/api/packages/opensanitizer/recent
```

### Step 3: Create Migration Guides

**OpenSanitizer → openadapt-privacy Migration Guide:**

```markdown
# Migration Guide: OpenSanitizer → openadapt-privacy

## Installation Change
```bash
# Old
pip install opensanitizer

# New
pip install openadapt-privacy
```

## API Changes
```python
# Old
from opensanitizer import scrub_text
result = scrub_text(text)

# New
from openadapt_privacy import scrub_text
result = scrub_text(text)
```

## New Features in openadapt-privacy
- Enhanced PII detection
- Healthcare PHI support
- Better performance
- Additional entity types
- Integration with OpenAdapt ecosystem

## Need Help?
Open an issue at: https://github.com/OpenAdaptAI/openadapt-privacy/issues
```

---

## PHASE 3: Final Archival (Month 2)
**Target:** Archive Phase 2 repositories after deprecation period
**Risk:** Low - 30-day notice period completed
**Time:** 2 hours

### Archival Date: [30 days after deprecation notice]

**Pre-Archive Checklist:**

```bash
# For each repository in Phase 2:

1. [ ] Verify deprecation notice was live for 30+ days
2. [ ] Review any issues/comments raised during deprecation
3. [ ] Confirm migration documentation is available
4. [ ] Update openadapt.ai documentation
5. [ ] Update internal documentation
6. [ ] Archive repository
7. [ ] Update PyPI/npm package metadata if applicable
8. [ ] Send final notice to any identified users
```

**Final Archive Notice:**

```markdown
# 🔒 ARCHIVED

This repository was archived on [DATE] after a 30-day deprecation period.

## Replacement
**[Replacement repository/location]**

## Migration Guide
See: [link to migration guide]

## Historical Reference
This archive is preserved for historical reference and will remain accessible
in read-only mode.

---
*For active OpenAdapt development, visit https://github.com/OpenAdaptAI/OpenAdapt*
```

---

## PHASE 4: Strategic Review (Month 3)
**Target:** Edge cases requiring decisions
**Risk:** Medium - strategic implications
**Time:** 1-2 weeks for evaluation and decision

### Repositories Requiring Decisions

#### 1. **app** Repository
**Status:** Empty (only LICENSE file)
**Forks:** 3
**Created:** January 11, 2025 (1 week ago)

**Options:**
- A. Archive immediately (no code exists)
- B. Set 30-day development deadline
- C. Set 90-day development deadline
- D. Transfer ownership to lead developer

**Recommendation:** Option B - 30-day deadline

**Action:**
```markdown
# Decision Needed

This repository was created for a desktop application but contains no code.

**Options:**
1. Begin development within 30 days
2. Archive repository

**Note:** Similar functionality now exists in openadapt-tray:
https://github.com/OpenAdaptAI/openadapt-tray

Please comment with intended use case or this repo will be archived on [DATE].
```

#### 2. **UI-TARS-desktop** Fork
**Status:** Fork with minimal changes
**Stars:** 1
**Created:** January 21, 2025

**Options:**
- A. Archive (low engagement, fork with minimal changes)
- B. Keep (recent creation, possible strategic value)
- C. Merge changes upstream and archive

**Recommendation:** Option A - Archive

**Rationale:**
- Created recently but minimal activity
- Fork with limited differentiation
- OpenAdapt has own GUI solutions (openadapt-tray)

#### 3. **atomacos** and **pynput** Forks
**Status:** Critical dependencies, stale (>18 months)
**Risk:** High if archived

**Options:**
- A. Keep as-is (maintain forks)
- B. Contribute changes upstream, use upstream
- C. Refresh forks with upstream updates

**Recommendation:** Option C - Refresh and maintain

**Action Plan:**
```bash
# For each fork (atomacos, pynput):

1. [ ] Document OpenAdapt-specific changes
2. [ ] Sync with upstream repository
3. [ ] Evaluate if changes can be upstreamed
4. [ ] If yes: Create PRs to upstream
5. [ ] If no: Maintain fork with regular syncs
6. [ ] Add note to README about fork purpose
7. [ ] Set up automated sync if keeping fork
```

---

## PHASE 5: Governance & Policy (Ongoing)

### Fork Management Policy

**Guidelines for Future Forks:**

1. **Before Creating a Fork:**
   - Document reason for fork vs. using upstream
   - Identify specific changes needed
   - Set maintenance expectations

2. **Fork Lifecycle:**
   - If no changes in 6 months → evaluate for archival
   - If changes can be upstreamed → create PR to upstream
   - If keeping fork → sync with upstream quarterly

3. **Fork Documentation:**
   ```markdown
   # Fork Notice

   This is a fork of [upstream-repo] maintained for [specific reason].

   **Modifications:**
   - [List of changes specific to OpenAdapt]

   **Sync Status:**
   - Last synced: [date]
   - Upstream version: [version/commit]

   **Maintenance:**
   - Sync schedule: [quarterly/as-needed]
   - Maintainer: [@username]
   ```

### Repository Creation Policy

**New Repository Checklist:**

```markdown
Before creating a new OpenAdaptAI repository:

- [ ] Defined purpose and scope
- [ ] README with clear description
- [ ] Initial code or clear development timeline
- [ ] Identified maintainer(s)
- [ ] Integration plan with OpenAdapt ecosystem
- [ ] License file (default: MIT)
- [ ] .github/CODEOWNERS file
- [ ] CI/CD setup plan

If creating a fork:
- [ ] Documented reason for fork vs. upstream
- [ ] List of intended modifications
- [ ] Upstream sync strategy
- [ ] Evaluation criteria for archive
```

---

## Success Metrics

### Pre-Cleanup (Current State)
- Total Repositories: 49
- Active (< 30 days): 13 (27%)
- Inactive (> 6 months): 30 (61%)
- Archived: 2 (4%)

### Post-Cleanup (Target State)
- Total Repositories: 26
- Active (< 30 days): 13 (50%)
- Strategic Keep (> 30 days): 11 (42%)
- Archived: 25 (48% of all repos)

### Key Performance Indicators
- ✅ Reduce maintenance burden by 47%
- ✅ Improve clarity for new contributors
- ✅ Clear migration paths for all archived projects
- ✅ Zero breaking changes for active users
- ✅ Preserved historical reference

---

## Communication Plan

### Internal Communication
1. **Team Meeting:** Present analysis and plan
2. **Review Period:** 1 week for feedback
3. **Approval:** Sign-off from project leads
4. **Execution:** Follow phased approach

### External Communication
1. **Blog Post:** Announce repository consolidation
2. **Social Media:** Share migration resources
3. **Documentation:** Update openadapt.ai
4. **Package Registries:** Update metadata

### Communication Template

```markdown
# OpenAdaptAI Repository Consolidation

As part of our transition to a modular architecture, we're consolidating our
GitHub repositories to focus on active projects and reduce maintenance overhead.

## What's Changing
- Archiving 23 research forks and obsolete projects
- Preserving all code in read-only archived state
- Providing migration guides for superseded projects

## What's Staying
- All active OpenAdapt packages (13 repos)
- Strategic dependencies and community projects (11 repos)
- Comprehensive documentation at openadapt.ai

## Timeline
- Phase 1 (Weeks 1-2): Archive inactive research forks
- Phase 2 (Weeks 3-4): Deprecation notices for superseded projects
- Phase 3 (Month 2): Final archival after 30-day notice period

## Migration Support
For help migrating from archived projects, see our migration guides:
[links to guides]

Questions? Open an issue at: https://github.com/OpenAdaptAI/OpenAdapt/issues
```

---

## Risk Mitigation

### Potential Risks

1. **Breaking External Dependencies**
   - **Risk:** External projects depend on archived repos
   - **Mitigation:** Archives remain accessible (read-only)
   - **Mitigation:** Deprecation notices with migration paths
   - **Mitigation:** Monitor for issues during deprecation period

2. **Lost Documentation**
   - **Risk:** Important docs only in archived repos
   - **Mitigation:** Audit docs before archival
   - **Mitigation:** Migrate critical docs to openadapt.ai
   - **Mitigation:** Archived repos remain accessible

3. **Community Backlash**
   - **Risk:** Users unhappy with archival
   - **Mitigation:** Clear communication
   - **Mitigation:** 30-day deprecation period for active projects
   - **Mitigation:** Migration guides and support

4. **Accidental Archival of Active Project**
   - **Risk:** Archive wrong repository
   - **Mitigation:** Multi-person review process
   - **Mitigation:** Dependency checking before archive
   - **Mitigation:** Test on low-risk repos first

### Rollback Plan

If archival causes unexpected issues:

```bash
# Unarchive repository
gh repo unarchive OpenAdaptAI/[repo-name]

# Restore any removed notices
git revert [commit-hash]

# Communicate restoration
# Post issue update/blog post
```

---

## Execution Tracking

### Phase 1 Progress (17 repos)

- [ ] whisper
- [ ] ultralytics
- [ ] grok-1
- [ ] Self-Rewarding-Language-Models
- [ ] Prompt-Engineering-Guide
- [ ] prismer
- [ ] active-prompt
- [ ] samapi
- [ ] R1-V
- [ ] Qwen2.5-VL
- [ ] open-r1-multimodal
- [ ] Janus
- [ ] CogVLM
- [ ] Awesome-LLM-Reasoning
- [ ] llama-agentic-system
- [ ] omniparser-api
- [ ] OmniMCP.web

**Start Date:** ___________
**Completion Date:** ___________
**Issues Encountered:** ___________

### Phase 2 Progress (5 repos)

- [ ] OpenSanitizer - Deprecation notice posted: ___________
- [ ] OpenReflector - Deprecation notice posted: ___________
- [ ] procdoc - Deprecation notice posted: ___________
- [ ] openadapt-gitbook - Deprecation notice posted: ___________
- [ ] oa.blog - Deprecation notice posted: ___________

**Deprecation Start:** ___________
**Archive Date:** ___________ (30 days later)
**Issues Raised:** ___________

### Phase 3 Progress (Strategic Decisions)

- [ ] app - Decision made: ___________
- [ ] UI-TARS-desktop - Decision made: ___________
- [ ] atomacos - Action taken: ___________
- [ ] pynput - Action taken: ___________

**Review Date:** ___________
**Decisions Finalized:** ___________

---

## Next Steps

1. **Immediate (This Week):**
   - [ ] Review this action plan with team
   - [ ] Get approval to proceed
   - [ ] Set start date for Phase 1

2. **Week 1-2 (Phase 1):**
   - [ ] Execute Phase 1 archival
   - [ ] Update documentation
   - [ ] Monitor for issues

3. **Week 3-4 (Phase 2 Start):**
   - [ ] Post deprecation notices
   - [ ] Create migration guides
   - [ ] Monitor for feedback

4. **Month 2 (Phase 3):**
   - [ ] Archive Phase 2 repositories
   - [ ] Update package registries
   - [ ] Publish communication

5. **Month 3 (Phase 4):**
   - [ ] Make strategic decisions
   - [ ] Implement governance policy
   - [ ] Complete consolidation

---

## Contact

**Questions or concerns about this action plan:**
- Open an issue: https://github.com/OpenAdaptAI/OpenAdapt/issues
- Contact: [team lead email/contact]

**This is a living document.** Updates will be tracked in version control.

---

*Last Updated: January 17, 2026*
