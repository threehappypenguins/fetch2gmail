# Publishing Fetch2Gmail (maintainer only)

## Test the build locally (before pushing)

Run this from the repo root so you catch build errors and deprecation warnings before pushing:

```bash
pip install build
python -m build
```

- This creates `dist/fetch2gmail-<version>.tar.gz` and `dist/*.whl`. If it fails or shows setuptools deprecation warnings, fix `pyproject.toml` and try again.
- Optional: upload to **Test PyPI** first (does not affect the real PyPI):
  ```bash
  pip install twine
  twine upload --repository testpypi dist/*
  ```
  Use your Test PyPI credentials (create an account at https://test.pypi.org if needed). Then try `pip install --index-url https://test.pypi.org/simple/ fetch2gmail` to verify the package installs.

## PyPI: Trusted Publishing

The workflow uses PyPI Trusted Publishing (no API token). On PyPI, add this repo as a Trusted Publisher:

- **Owner:** your GitHub username or org (e.g. `threehappypenguins`)
- **Repository:** `fetch2gmail`
- **Workflow name:** `publish-pypi.yml`

Remove the `PYPI_API_TOKEN` secret from the repo so the action uses Trusted Publishing only.

## Creating a release

1. Bump `version` in `pyproject.toml` (e.g. `1.0.1`), commit and **push to the default branch**.
2. On GitHub: **Releases** → **Create a new release**:
   - **Important:** Create the tag **from the latest commit** that has the new version. Click "Choose a tag" → type the new tag (e.g. `v1.0.1`) → choose **"Create new tag: v1.0.1 on publish"** and select the **default branch** (e.g. `main`) so the tag points to the commit you just pushed. If you create the tag from an old commit, the workflow will build the old version and the upload will fail (e.g. "File already exists").
   - Release title: e.g. `v1.0.1` → **Publish release**.
3. The **Publish to PyPI** workflow runs. In the Actions log, the "Verify version" step should show the version you expect. Check the **Actions** tab for success.

You cannot re-upload a version that already exists on PyPI; use a new version number.

**If you see "an explicit password was also set":** Remove the **PYPI_API_TOKEN** secret from the repo (Settings → Secrets and variables → Actions). The workflow uses Trusted Publishing only when no password/token is passed.
