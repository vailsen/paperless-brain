# Third-party notices

PaperlessBrain is licensed under the [MIT License](LICENSE). It depends on the
following third-party packages, all under permissive licenses compatible with
MIT redistribution.

## Direct dependencies

| Package | License |
|---|---|
| aiohttp | Apache-2.0 AND MIT |
| anthropic | MIT |
| chromadb | Apache-2.0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| fastapi | MIT |
| httpx | BSD-3-Clause |
| lxml | BSD-3-Clause |
| markdown2 | MIT |
| nicegui | MIT |
| pillow | MIT-CMU |
| pydantic | MIT |
| pydantic-settings | MIT |
| pypdfium2 | BSD-3-Clause / Apache-2.0 |
| python-docx | MIT |
| pyyaml | MIT |
| requests | Apache-2.0 |
| sentence-transformers | Apache-2.0 |
| snowballstemmer | BSD-3-Clause |
| trafilatura | Apache-2.0 |
| weasyprint | BSD-3-Clause |

Optional extras: `crawl4ai` (Apache-2.0, `[crawl]`), `babel` (BSD-3-Clause, `[i18n]`).

## Notable transitive dependencies

| Package | License | Note |
|---|---|---|
| torch | BSD-3-Clause | via sentence-transformers |
| transformers | Apache-2.0 | via sentence-transformers |
| uvicorn, starlette | BSD-3-Clause | via fastapi / nicegui |
| fonttools | MIT | via weasyprint |
| pydyf, tinycss2, cssselect2 | BSD-3-Clause | via weasyprint |
| pyphen | MPL-1.1 | see below |

**pyphen** (hyphenation dictionaries, pulled in by WeasyPrint) is tri-licensed
GPLv2+ / LGPLv2+ / MPL-1.1. PaperlessBrain uses it under the **MPL-1.1** option.
MPL-1.1 is file-level copyleft: it applies only to modifications of pyphen's own
source files. pyphen is used unmodified, so no obligation extends to this
project's code.

## Bundled assets

The embedding model (`intfloat/multilingual-e5-large-instruct`, MIT) is
downloaded at runtime from Hugging Face and is not redistributed with this
project.

## Related projects

PaperlessBrain communicates with [Paperless-ngx](https://docs.paperless-ngx.com/)
(GPL-3.0) over its public REST API only. No Paperless-ngx code is included or
linked, so its license does not extend to this project.
