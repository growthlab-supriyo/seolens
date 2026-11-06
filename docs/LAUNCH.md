# Launch checklist: SEO Lens 0.1.0

Three places, in this order: **GitHub** (the code), **PyPI** (`pipx install seolens`), **Hugging Face** (the online demo).
Accounts: GitHub `growthlab-supriyo`. Everything below is free.

## 1. GitHub: publish the code

1. Open https://github.com/new while signed in as `growthlab-supriyo`.
2. Repository name: `seolens` · Description: `Free, open-source SEO crawler and site audit tool (up to 10,000 URLs)` · **Public**.
3. Leave **Add a README**, **.gitignore** and **license** unticked (the project already has them). Click **Create repository**.
4. Push the code (Windows opens a GitHub sign-in window the first time):

   ```bash
   cd "C:\Users\supri\OneDrive\Documents\seolens"
   git remote add origin https://github.com/growthlab-supriyo/seolens.git
   git push -u origin main
   ```
5. Open the **Actions** tab: the `tests` workflow runs on Windows and Ubuntu. Wait for green ticks.
6. Repository page → the gear next to **About**: add topics `seo`, `seo-audit`, `crawler`, `technical-seo`, `streamlit`, `python`.
7. **Settings → Code security → Private vulnerability reporting → Enable** (SECURITY.md points people there).

## 2. PyPI: make `pipx install seolens` work

1. Create an account at https://pypi.org/account/register/ and turn on two-factor authentication (PyPI requires it).
2. https://pypi.org/manage/account/publishing/ → **Add a new pending publisher** → GitHub:
   - PyPI project name: `seolens`
   - Owner: `growthlab-supriyo` · Repository name: `seolens`
   - Workflow name: `release.yml` · Environment name: `pypi`
3. On GitHub: **Settings → Environments → New environment** → name it `pypi`.
4. On GitHub: **Releases → Draft a new release** → **Choose a tag** → type `v0.1.0` → **Create new tag** → title `SEO Lens 0.1.0`,
   paste the 0.1.0 notes from CHANGELOG.md → **Publish release**.
5. **Actions** tab: the `release` workflow builds and uploads the package (no password or token is stored anywhere).
6. Check it from any computer:

   ```bash
   pipx install seolens
   seolens
   ```

## 3. Hugging Face: the online demo (500 URLs)

Do this after step 2: the demo installs the published package.

1. Create an account at https://huggingface.co/join.
2. https://huggingface.co/new-space → Space name `seolens` · License MIT · SDK **Docker** → **Blank** · Hardware **CPU basic (free)** · **Public** → **Create Space**.
3. In the Space: **Files → Add file → Upload files** → upload `deploy/huggingface/Dockerfile` and `deploy/huggingface/README.md`
   (the README replaces the Space's own one) → **Commit**.
4. The Space builds for a few minutes, then opens at `https://<your-hf-username>-seolens.hf.space`.
5. Add the demo link to the GitHub README (line under the title) and to the repository's **About → Website**.

Free Spaces go to sleep after about 48 hours without visitors and wake on the next visit (about 30 seconds).

## 4. After launch

- Share it: a LinkedIn post with a screenshot of the Overview and a sample Excel (from a site you own).
- Releases: raise `version` in `pyproject.toml` and `seolens/__init__.py`, add a CHANGELOG entry, commit, then publish a new GitHub
  release `vX.Y.Z`; PyPI updates by itself. Update the version in `deploy/huggingface/Dockerfile` and re-upload it.
- Optional later: a signed Windows .exe through SignPath Foundation (free for open-source projects).
