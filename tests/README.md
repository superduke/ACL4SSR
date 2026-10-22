# List updater checks

Run the network-independent regression suite with Python 3.10+ and Git:

```sh
python -m unittest discover -s tests -v
bash -n scripts/commit-list-updates.sh
```

The `Test list updaters` pull-request workflow runs the same checks with read-only repository permissions. Tests use mocked downloads and disposable local Git repositories, never the real update branches.

Coverage includes valid/changed/unchanged lists; missing, empty, malformed, HTML, and partial responses; renamed archive roots; missing ASN sources; preservation of existing outputs on failed downloads or validation; and committing/pushing only the intended files. The tests do not run the scheduled update workflows.

## Source configuration

The baipiao workflow reads `PROXYIP_URL` from repository Actions Variables into the job environment. Set it to a complete `https://…` (or `http://…`) URL that returns a UTF-8 plain-text IP list. A bare proxy hostname is not a list URL. HTTP 200 alone is insufficient: every nonblank, noncomment entry must contain a valid IP, optionally a port and `#label`. IPv6 endpoints with ports must use `[IPv6]:port`. Empty/comment-only, HTML, JSON, and malformed lists fail before replacing `baipiao.txt`.

Adobe's source must contain `0.0.0.0 domain.example` hosts entries (plus blank lines/comments). Cloudflare uses `ipverse/as-ip-blocks` and the existing set of eight ASNs; all selected source files must exist with matching ASN headers. Comment-only individual ASN files are permitted, but the combined IPv4 list must be nonempty.

All three workflows fail on download/validation errors, retain the last good outputs, and skip commit/push successfully when their outputs are unchanged. Existing repository credentials and settings are not changed.
