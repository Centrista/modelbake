# Publish ModelBake to PyPI

The release workflow uses PyPI trusted publishing. It has no stored PyPI token.
The build and publish jobs are separate, and only the publish job can request an
OIDC identity token.

Nothing publishes automatically. The workflow runs only from `main`, requires
the exact confirmation `PUBLISH MODELBAKE 0.1.0 TO PYPI`, and then pauses at the
GitHub `pypi` environment if that environment requires approval.

## One-time setup

After the public GitHub repository exists, create a pending trusted publisher
for the `modelbake-ai` project on PyPI with these exact values:

| PyPI field | Value |
| --- | --- |
| PyPI project name | `modelbake-ai` |
| GitHub owner | `Centrista` |
| GitHub repository | `modelbake` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

In the GitHub repository, create an environment named `pypi`. Add an approval
rule before the first release so the OIDC publishing job cannot start without a
second explicit action.

## First release

1. Confirm the public repository is on the exact reviewed `main` commit.
2. Run the existing **Release candidate** workflow first and inspect its
   uploaded checksums and distributions.
3. Open **Publish ModelBake to PyPI** in GitHub Actions.
4. Run it from `main` and enter `PUBLISH MODELBAKE 0.1.0 TO PYPI` exactly.
5. Review the `pypi` environment gate and approve it only if the build job
   passed from the expected commit.
6. Verify the PyPI page, hashes, provenance attestations, and a clean install:

   ```bash
   python -m pip install modelbake-ai==0.1.0
   modelbake tour
   ```

Do not set `skip-existing`. A duplicate or unexpected release must fail loudly.
Do not add a PyPI password or API token to this workflow.
