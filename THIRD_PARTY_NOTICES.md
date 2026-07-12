# Third-party software notices

Runnywhere source code is MIT-licensed. Its installed runtime also contains the
following direct dependencies and their transitive dependencies. Copyright
notices and licence texts distributed inside each Python package remain in the
container and must be preserved when redistributing it.

| Component | Licence | Project |
|---|---|---|
| MCP Python SDK | MIT | https://github.com/modelcontextprotocol/python-sdk |
| NetworkX | BSD-3-Clause | https://networkx.org/ |
| NumPy | BSD-3-Clause and bundled component licences | https://numpy.org/ |
| Uvicorn | BSD-3-Clause | https://www.uvicorn.org/ |
| Starlette | BSD-3-Clause | https://www.starlette.io/ |
| Pydantic / pydantic-core | MIT | https://github.com/pydantic/pydantic |
| AnyIO | MIT | https://github.com/agronholm/anyio |
| HTTPX / HTTPCore | BSD-3-Clause | https://www.python-httpx.org/ |
| sse-starlette | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| jsonschema, referencing, rpds-py | MIT | https://github.com/python-jsonschema/jsonschema |
| Click | BSD-3-Clause | https://github.com/pallets/click |
| h11 | MIT | https://github.com/python-hyper/h11 |
| Pretendard Variable 1.3.9 | SIL Open Font License 1.1 | https://github.com/orioncactus/pretendard |

The `python:3.12-slim` base image and its Debian packages retain their respective
upstream licences. A distributor of a built image should generate an SBOM and
retain the package licence files under the image's standard documentation paths.

Data licences, including ODbL and KOGL attribution, are documented separately in
`DATA_LICENSES.md`. Kakao Maps and Local APIs are network services governed by
the Kakao Developers terms rather than bundled software.

The self-hosted Pretendard font file is distributed with its licence text at
`src/runart/assets/Pretendard-OFL.txt`. “Pretendard” is a Reserved Font Name
under that licence.
