# Release notes

Each release has one file here, named after its tag, such as `v0.1.0.md`. The first line is the release title, then a blank line, then the description. `{{SHA256}}` is replaced with the zip's SHA-256.

To publish a release, commit its notes, then push the tag:

```bash
git tag v0.1.1
git push origin v0.1.1
```

`.github/workflows/release.yml` then builds `Roostfile-windows-x64.zip` on a clean Windows machine from python.org's embeddable Python 3.12.7 and `requirements-ui.txt`, unzips it into a folder whose name has a space and Chinese characters, renders the demo window once, and publishes the release. If any step fails, nothing is published; fix it, delete the tag (`git push origin :refs/tags/v0.1.1`, then `git tag -d v0.1.1`) and push it again.

`.github/python-3.12.7-embed-amd64.sha256` lists the runtime files of the copy that was tested by hand. To move to another Python, test a copy by hand first, then replace that file and `EMBED_VERSION` / `EMBED_MD5` in the workflow.
