# TestPyPI Trusted Publisher Setup

## Current Issue

The GitHub Actions workflow fails at the TestPyPI publishing step with:

```
Token request failed: the server refused the request for the following reasons:
* `invalid-publisher`: valid token, but no corresponding publisher
```

**Impact**: Workflow shows as "failed" even though the main PyPI publish succeeds.

## Solution: Configure Trusted Publisher on TestPyPI

### Step 1: Log in to TestPyPI

Go to https://test.pypi.org/ and log in with your account.

### Step 2: Navigate to Publishing Settings

1. Click on your username in the top-right
2. Select "Account settings" from the dropdown
3. Scroll down to "Publishing" section
4. Click "Add a new pending publisher"

### Step 3: Configure the Trusted Publisher

Enter these exact values:

| Field | Value |
|-------|-------|
| **PyPI Project Name** | `openadapt-evals` |
| **Owner** | `OpenAdaptAI` |
| **Repository name** | `openadapt-evals` |
| **Workflow name** | `publish.yml` |
| **Environment name** | `testpypi` |

### Step 4: Save Configuration

Click "Add" to save the trusted publisher configuration.

### Step 5: Test the Configuration

After configuration, test by creating and pushing a test tag:

```bash
cd /Users/abrichr/oa/src/openadapt-evals

# Create a test tag
git tag v0.1.1-test

# Push the tag to trigger the workflow
git push origin v0.1.1-test

# Monitor the workflow
gh run watch

# After testing, clean up the test tag
git tag -d v0.1.1-test
git push origin :refs/tags/v0.1.1-test
```

## Alternative: Make TestPyPI Optional

If you don't need TestPyPI publishing, you can make it optional so the workflow doesn't fail:

### Option A: Make TestPyPI Non-Blocking

Edit `.github/workflows/publish.yml` and add `continue-on-error: true`:

```yaml
publish-to-testpypi:
  name: Publish to TestPyPI
  needs: build
  runs-on: ubuntu-latest
  continue-on-error: true  # Add this line
  environment:
    name: testpypi
    url: https://test.pypi.org/p/openadapt-evals
  permissions:
    id-token: write
  # ... rest unchanged
```

### Option B: Skip TestPyPI Entirely

Comment out or remove the entire `publish-to-testpypi` job in `.github/workflows/publish.yml`.

### Option C: Only Publish to TestPyPI on Pre-release Tags

Modify the workflow to only publish to TestPyPI for pre-release tags (e.g., `v0.1.0-alpha`, `v0.1.0-rc1`):

```yaml
publish-to-testpypi:
  name: Publish to TestPyPI
  needs: build
  runs-on: ubuntu-latest
  if: contains(github.ref, '-')  # Only run for tags with hyphen (pre-releases)
  environment:
    name: testpypi
    url: https://test.pypi.org/p/openadapt-evals
  permissions:
    id-token: write
  # ... rest unchanged
```

## Why Use TestPyPI?

TestPyPI is useful for:
- Testing the packaging and upload process before releasing to production PyPI
- Verifying package metadata and installation
- Testing with `pip install --index-url https://test.pypi.org/simple/ openadapt-evals`

However, it's **not required** for production publishing. Many projects skip TestPyPI entirely.

## Verification

After setup, verify TestPyPI configuration is working:

1. **Check the workflow run**: Visit https://github.com/OpenAdaptAI/openadapt-evals/actions
   - All jobs (including TestPyPI) should show green checkmarks

2. **Verify package on TestPyPI**: Visit https://test.pypi.org/project/openadapt-evals/
   - Package should be visible with the test version

3. **Test installation from TestPyPI**:
   ```bash
   pip install --index-url https://test.pypi.org/simple/ openadapt-evals
   ```

## Troubleshooting

### "Publisher with matching claims was not found"

**Cause**: Trusted publisher not configured or configured incorrectly.

**Solution**:
1. Double-check all fields match exactly (case-sensitive)
2. Ensure the GitHub environment name is `testpypi` (lowercase)
3. Verify workflow filename is `publish.yml` (not `.github/workflows/publish.yml`)

### Workflow Still Failing After Configuration

**Possible causes**:
1. Configuration not saved properly - re-add the trusted publisher
2. Wrong repository owner - should be `OpenAdaptAI`, not personal username
3. GitHub environment not created - check https://github.com/OpenAdaptAI/openadapt-evals/settings/environments

### Package Already Exists on TestPyPI

TestPyPI doesn't allow re-uploading the same version. Either:
- Bump the version number before testing
- Delete the package on TestPyPI and re-upload
- Use the `continue-on-error: true` approach to skip TestPyPI failures

## References

- PyPI Trusted Publishers: https://docs.pypi.org/trusted-publishers/
- TestPyPI: https://test.pypi.org/help/#publishing
- GitHub Environments: https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment
