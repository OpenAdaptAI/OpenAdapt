# PyPI Publishing Plan for openadapt-evals

## Current Status Analysis (as of 2026-01-17)

### ✅ What's Working

1. **Package IS Published on PyPI**
   - Version: 0.1.0
   - Published: 2026-01-17 00:04:04 UTC
   - URL: https://pypi.org/project/openadapt-evals/
   - Successfully installable via `pip install openadapt-evals`

2. **GitHub Actions Workflow Exists**
   - Location: `.github/workflows/publish.yml`
   - Uses trusted publishing (OIDC) - no API tokens needed
   - Automatically publishes on tag push (e.g., `v*`)
   - Successfully published to PyPI on v0.1.0 tag

3. **Package Configuration is Correct**
   - `pyproject.toml` is properly configured
   - Package name: `openadapt-evals` ✓
   - Build system: `hatchling` ✓
   - Python requirement: >=3.10 ✓
   - Local build test: ✅ PASSED

4. **GitHub Trusted Publisher (PyPI)**
   - ✅ Successfully configured on PyPI
   - ✅ Working for main PyPI publishing

### ⚠️ Issues Found

#### Issue 1: Downloads Badge Shows "Package Not Found"

**Root Cause**: Newly published package (16 hours old) - PyPI stats services (like pypistats.org) take time to index new packages.

**Evidence**:
- Badge URL: `https://img.shields.io/pypi/dm/openadapt-evals.svg`
- Badge shows: "package not found" (red)
- PyPI Stats API: 404 response for `openadapt-evals`
- Package exists on PyPI: ✓ (verified via JSON API)

**Resolution**:
- **No action needed** - this will resolve automatically within 24-48 hours as PyPI stats services index the new package
- Alternative: Use different badge service or wait for stats to populate

#### Issue 2: TestPyPI Publishing Failed

**Root Cause**: Trusted publisher not configured on TestPyPI

**Evidence**:
```
Token request failed: the server refused the request for the following reasons:
* `invalid-publisher`: valid token, but no corresponding publisher (Publisher with matching claims was not found)
```

**GitHub environment**: `testpypi` exists but trusted publisher not configured on TestPyPI.org

**Impact**:
- Workflow shows as "failed" even though main PyPI publish succeeded
- Cannot test publishing flow on TestPyPI before production

**Resolution Required**: Configure trusted publisher on TestPyPI

#### Issue 3: GitHub Release Creation Succeeded Despite TestPyPI Failure

**Status**: The workflow has a dependency issue where GitHub release was created despite TestPyPI failure

**Current workflow logic**:
```yaml
github-release:
  needs: publish-to-pypi  # Only depends on PyPI, not TestPyPI
```

**Impact**: Minor - GitHub releases still created, but workflow shows as failed overall

## Action Plan

### Priority 1: Fix TestPyPI Trusted Publisher (Required for Clean Workflow)

**Steps**:
1. Log in to https://test.pypi.org/
2. Navigate to Account Settings → Publishing
3. Add trusted publisher with these details:
   - PyPI Project Name: `openadapt-evals`
   - Owner: `OpenAdaptAI`
   - Repository name: `openadapt-evals`
   - Workflow name: `publish.yml`
   - Environment name: `testpypi`

**Testing**:
```bash
# After configuration, test by creating a new tag
git tag v0.1.1-test
git push origin v0.1.1-test
# Then delete tag after testing
git tag -d v0.1.1-test
git push origin :refs/tags/v0.1.1-test
```

### Priority 2: Update Badge URL (Optional - Can Wait 24-48h)

**Option A: Wait for Stats to Populate (Recommended)**
- Check again in 24-48 hours
- No changes needed if stats populate automatically

**Option B: Use Alternative Badge Service**
Update README.md badge to use PePy:
```markdown
[![Downloads](https://static.pepy.tech/badge/openadapt-evals/month)](https://pepy.tech/project/openadapt-evals)
```

**Option C: Remove Downloads Badge Temporarily**
Remove the downloads badge until stats are available, then re-add later.

### Priority 3: Improve Workflow Robustness (Recommended)

**Update `.github/workflows/publish.yml`**:

Option A - Make TestPyPI optional (allow workflow to succeed even if TestPyPI fails):
```yaml
publish-to-testpypi:
  name: Publish to TestPyPI
  needs: build
  runs-on: ubuntu-latest
  continue-on-error: true  # Add this line
  environment:
    name: testpypi
    url: https://test.pypi.org/p/openadapt-evals
  # ... rest of job
```

Option B - Skip TestPyPI entirely (if not needed):
```yaml
# Comment out or remove the publish-to-testpypi job
```

Option C - Make GitHub release depend on both (current issue):
```yaml
github-release:
  name: Create GitHub Release
  needs: [publish-to-pypi, publish-to-testpypi]  # Add testpypi dependency
  # ... rest of job
```

## Publishing Process Documentation

### First-Time Setup (Already Complete ✓)

1. ✅ Create `pyproject.toml` with package metadata
2. ✅ Set up GitHub Actions workflow (`.github/workflows/publish.yml`)
3. ✅ Configure trusted publisher on PyPI
4. ⚠️ Configure trusted publisher on TestPyPI (TO DO)

### Publishing New Versions

**Automatic Publishing (via GitHub Actions)**:

1. Update version in `pyproject.toml`:
   ```toml
   [project]
   version = "0.1.1"  # Increment version
   ```

2. Commit changes:
   ```bash
   git add pyproject.toml
   git commit -m "chore: bump version to 0.1.1"
   ```

3. Create and push tag:
   ```bash
   git tag v0.1.1
   git push origin main
   git push origin v0.1.1
   ```

4. GitHub Actions will automatically:
   - Build the package
   - Publish to PyPI (and TestPyPI once configured)
   - Create GitHub release with artifacts

**Manual Publishing (Backup Method)**:

If GitHub Actions fails or for testing:

```bash
# Build package
uv build

# Upload to TestPyPI (requires account/token)
uv publish --index-url https://test.pypi.org/legacy/

# Upload to PyPI (requires account/token - NOT RECOMMENDED, use trusted publishing)
uv publish
```

### Pre-Release Checklist

Before creating a new release tag:

- [ ] All tests passing
- [ ] Version bumped in `pyproject.toml`
- [ ] CLAUDE.md updated with changes (if needed)
- [ ] README.md updated (if needed)
- [ ] Changelog/release notes prepared
- [ ] Local build test: `uv build` succeeds
- [ ] Test installation: `pip install dist/*.whl` works

### Post-Release Verification

After pushing a new tag:

1. Check GitHub Actions workflow: https://github.com/OpenAdaptAI/openadapt-evals/actions
2. Verify PyPI page: https://pypi.org/project/openadapt-evals/
3. Test installation: `pip install --upgrade openadapt-evals`
4. Verify version: `pip show openadapt-evals`
5. Check GitHub release: https://github.com/OpenAdaptAI/openadapt-evals/releases

## Badges Explanation

Current badges in README.md:

1. **Build Status**: Shows GitHub Actions workflow status
   - Source: GitHub Actions
   - Updates: Real-time
   - URL: `![Build Status](https://github.com/OpenAdaptAI/openadapt-evals/workflows/Publish%20to%20PyPI/badge.svg?branch=main)`

2. **PyPI Version**: Shows current version on PyPI
   - Source: PyPI API
   - Updates: Immediate after publish
   - URL: `![PyPI version](https://img.shields.io/pypi/v/openadapt-evals.svg)`
   - Status: ✅ Working

3. **Downloads**: Shows monthly download count
   - Source: PyPI Stats / shields.io
   - Updates: Daily (with 24-48h delay for new packages)
   - URL: `![Downloads](https://img.shields.io/pypi/dm/openadapt-evals.svg)`
   - Status: ⚠️ "Package not found" (temporary, will resolve)

4. **License**: Static badge
   - Status: ✅ Working

5. **Python Version**: Static badge showing Python requirement
   - Status: ✅ Working

## Testing the Package

### Test Installation from PyPI

```bash
# Create fresh virtual environment
python -m venv test_env
source test_env/bin/activate  # On Windows: test_env\Scripts\activate

# Install from PyPI
pip install openadapt-evals

# Verify installation
python -c "from openadapt_evals import ApiAgent, WAAMockAdapter; print('✓ Package imported successfully')"

# Test CLI
openadapt-evals --help

# Deactivate and clean up
deactivate
rm -rf test_env
```

### Test Mock Evaluation

```bash
# Install with test dependencies
pip install openadapt-evals[test]

# Run mock evaluation
python -m openadapt_evals.benchmarks.cli mock --tasks 5
```

## Troubleshooting

### "Package not found" on PyPI
- Check spelling: `openadapt-evals` (with hyphen)
- Verify on PyPI: https://pypi.org/project/openadapt-evals/
- Check package name in `pyproject.toml`: `name = "openadapt-evals"`

### Downloads Badge Not Working
- **If package is less than 48 hours old**: This is normal, wait for stats to populate
- **If package is older**: Check PyPI stats API: https://pypistats.org/api/packages/openadapt-evals/recent
- Consider alternative badge service: https://pepy.tech/project/openadapt-evals

### Workflow Failed but Package Published
- Check which job failed (build, pypi, testpypi, or github-release)
- If only TestPyPI failed: Main package is published, TestPyPI config needed
- If PyPI publish failed: Package not published, check trusted publisher config

### Trusted Publishing Not Working
1. Verify GitHub environment name matches workflow
2. Check PyPI trusted publisher configuration
3. Ensure repository name and owner are correct
4. Verify workflow filename is exact: `publish.yml`

## Related Documentation

- PyPI Trusted Publishers: https://docs.pypi.org/trusted-publishers/
- TestPyPI Setup: https://test.pypi.org/help/#publishing
- GitHub Actions: https://docs.github.com/en/actions
- uv documentation: https://docs.astral.sh/uv/

## Summary

**Current State**:
- ✅ Package successfully published to PyPI (v0.1.0)
- ✅ Installable via `pip install openadapt-evals`
- ⚠️ Downloads badge shows "package not found" (temporary - will resolve in 24-48h)
- ❌ TestPyPI publishing fails due to missing trusted publisher config
- ✅ GitHub Actions workflow exists and mostly works

**Immediate Actions Required**:
1. Configure TestPyPI trusted publisher (Priority 1)
2. Wait 24-48 hours for download stats to populate OR use alternative badge (Priority 2)
3. Consider making TestPyPI optional in workflow (Priority 3)

**No Action Needed**:
- Package publishing to PyPI is working correctly
- Build process is working correctly
- Package configuration is correct
