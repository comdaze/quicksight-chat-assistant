Third-party browser scripts, served by the web Lambda under `/static/<file>` so the chat page needs no CDN.
Each file is copied unmodified from the npm package below; the integrity value is the npm tarball's `sha512`.

| File | Source | npm integrity | License |
|---|---|---|---|
| `echarts-6.1.0.min.js` | echarts@6.1.0 `dist/echarts.min.js` | `sha512-q0yaFPggC9FUdsWH4blavRWFmxdrIodbkoKNAjJudAI6CA9gNPxHtV2RcZNEepZVlk4yvBYkOkbk6HIVpIyHZA==` | Apache-2.0 (`ECHARTS-LICENSE`) |
| `marked-18.1.1.umd.js` | marked@18.1.1 `lib/marked.umd.js` | `sha512-zEGgrtUG8crk+TrMacJSVABU4DYDDkS8uqgU3ohrn/2fW9C2AEfUrO3m6IhpIpQpl7YcVpiXXC9cSMWDANT3dg==` | MIT (`MARKED-LICENSE`) |
| `purify-3.4.17.min.js` | dompurify@3.4.17 `dist/purify.min.js` | `sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==` | Apache-2.0 or MPL-2.0 (`DOMPURIFY-LICENSE`) |

To upgrade: download the tarball with `npm pack <name>@<version>`, check its integrity against `npm view <name>@<version> dist.integrity`,
copy the file with the new versioned name, and update the file name in `web/handler.py`, `web/index.html` and `qsca`.
