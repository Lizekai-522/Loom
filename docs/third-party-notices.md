# 第三方依赖与许可证声明

初次记录：2026-10-22；S2 更新：2026-10-23（Asia/Shanghai）。本清单来自项目隔离环境中已安装发行包的 `METADATA` 和随包许可证文件；仅复制许可与署名文本，没有复制上游实现代码。安装器 pip 不属于应用依赖，未纳入。

应用自己的许可证尚未在此文件选定。分发时保留相应许可证、版权与署名原文；具体条款以随包文件为准。当前源码仓库没有内嵌这些发行包的实现，也没有打包本地 NATS Server/PostgreSQL 的 EXE、DLL 或数据目录。

## 已安装依赖与作用范围

保留首轮 23 个包，并新增 S2 的 7 个包，共 **30 个发行包、36 份许可/署名文件**。FastAPI、Pydantic、Uvicorn 为首轮直接运行依赖；nats-py 与 cryptography 用于 S2 消息和检查点能力，cffi/pycparser 为其传递依赖。psycopg、psycopg-binary 与 tzdata 属于 PostgreSQL 集成环境驱动及其依赖，不能据已安装就声称默认 API 已切换 PostgreSQL。HTTPX 从 0.3.0 起也是实际模型网关的直接运行依赖；pytest 仅用于验证。HTTPX 的传递依赖作用范围同步更新，包版本与许可原文不变。实际安装选择与版本以项目依赖配置、锁文件和对应阶段测试为准。

| 包 | 已安装版本 | 许可证 | 作用范围 | 官方来源 | 本地许可原文 |
|---|---|---|---|---|---|
| annotated-doc | `0.0.5` | MIT | runtime-transitive | [上游](https://github.com/fastapi/annotated-doc) | [LICENSE](../third_party/licenses/annotated-doc-0.0.5/LICENSE) |
| annotated-types | `0.8.0` | MIT | runtime-transitive | [上游](https://github.com/annotated-types/annotated-types) | [LICENSE](../third_party/licenses/annotated-types-0.8.0/LICENSE) |
| anyio | `4.15.1` | MIT | runtime-transitive | [上游](https://github.com/agronholm/anyio) | [LICENSE](../third_party/licenses/anyio-4.15.1/LICENSE) |
| certifi | `2026.10.19` | MPL-2.0 | runtime-transitive | [上游](https://github.com/certifi/python-certifi) | [LICENSE](../third_party/licenses/certifi-2026.10.19/LICENSE) |
| cffi | `2.1.1` | MIT-0 | S2-runtime-transitive | [上游](https://github.com/python-cffi/cffi) | [LICENSE](../third_party/licenses/cffi-2.1.1/LICENSE) |
| click | `8.5.0` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/pallets/click/) | [LICENSE.txt](../third_party/licenses/click-8.5.0/LICENSE.txt) |
| colorama | `0.4.6` | BSD-3-Clause (from packaged LICENSE.txt) | runtime-transitive | [上游](https://github.com/tartley/colorama) | [LICENSE.txt](../third_party/licenses/colorama-0.4.6/LICENSE.txt) |
| cryptography | `50.0.2` | Apache-2.0 OR BSD-3-Clause | S2-runtime | [上游](https://github.com/pyca/cryptography/) | [LICENSE](../third_party/licenses/cryptography-50.0.2/LICENSE), [LICENSE.APACHE](../third_party/licenses/cryptography-50.0.2/LICENSE.APACHE), [LICENSE.BSD](../third_party/licenses/cryptography-50.0.2/LICENSE.BSD) |
| fastapi | `0.159.1` | MIT | direct-runtime | [上游](https://github.com/fastapi/fastapi) | [LICENSE](../third_party/licenses/fastapi-0.159.1/LICENSE) |
| h11 | `0.16.0` | MIT | runtime-transitive | [上游](https://github.com/python-hyper/h11) | [LICENSE.txt](../third_party/licenses/h11-0.16.0/LICENSE.txt) |
| httpcore | `1.0.9` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/encode/httpcore) | [LICENSE.md](../third_party/licenses/httpcore-1.0.9/LICENSE.md) |
| httpx | `0.28.1` | BSD-3-Clause | direct-runtime-and-test | [上游](https://github.com/encode/httpx) | [LICENSE.md](../third_party/licenses/httpx-0.28.1/LICENSE.md) |
| idna | `3.20` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/kjd/idna) | [LICENSE.md](../third_party/licenses/idna-3.20/LICENSE.md) |
| iniconfig | `2.3.1` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/iniconfig) | [LICENSE](../third_party/licenses/iniconfig-2.3.1/LICENSE) |
| nats-py | `2.16.0` | Apache-2.0 | S2-runtime | [上游](https://github.com/nats-io/nats.py) | [LICENSE](../third_party/licenses/nats-py-2.16.0/LICENSE) |
| opentelemetry-api | `1.45.1` | Apache-2.0 | runtime-transitive | [上游](https://github.com/open-telemetry/opentelemetry-python) | [LICENSE](../third_party/licenses/opentelemetry-api-1.45.1/LICENSE) |
| packaging | `26.3` | Apache-2.0 OR BSD-2-Clause | test-or-test-transitive | [上游](https://github.com/pypa/packaging) | [LICENSE](../third_party/licenses/packaging-26.3/LICENSE), [LICENSE.APACHE](../third_party/licenses/packaging-26.3/LICENSE.APACHE), [LICENSE.BSD](../third_party/licenses/packaging-26.3/LICENSE.BSD) |
| pluggy | `1.7.0` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/pluggy) | [LICENSE](../third_party/licenses/pluggy-1.7.0/LICENSE) |
| psycopg | `3.3.6` | LGPL-3.0-only | postgres-integration-driver | [上游](https://github.com/psycopg/psycopg) | [LICENSE.txt](../third_party/licenses/psycopg-3.3.6/LICENSE.txt) |
| psycopg-binary | `3.3.6` | LGPL-3.0-only | postgres-integration-driver-binary | [上游](https://github.com/psycopg/psycopg) | [LICENSE.txt](../third_party/licenses/psycopg-binary-3.3.6/LICENSE.txt) |
| pycparser | `3.12` | BSD-3-Clause | S2-runtime-transitive | [上游](https://github.com/eliben/pycparser) | [LICENSE](../third_party/licenses/pycparser-3.12/LICENSE) |
| pydantic | `2.14.1` | MIT | direct-runtime | [上游](https://github.com/pydantic/pydantic) | [LICENSE](../third_party/licenses/pydantic-2.14.1/LICENSE) |
| pydantic_core | `2.50.1` | MIT | runtime-transitive | [上游](https://github.com/pydantic/pydantic/tree/main/pydantic-core) | [LICENSE](../third_party/licenses/pydantic-core-2.50.1/LICENSE) |
| Pygments | `2.21.0` | BSD-2-Clause | test-or-test-transitive | [上游](https://github.com/pygments/pygments) | [AUTHORS](../third_party/licenses/pygments-2.21.0/AUTHORS), [LICENSE](../third_party/licenses/pygments-2.21.0/LICENSE) |
| pytest | `9.1.1` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/pytest) | [LICENSE](../third_party/licenses/pytest-9.1.1/LICENSE) |
| starlette | `1.9.0` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/Kludex/starlette) | [LICENSE.md](../third_party/licenses/starlette-1.9.0/LICENSE.md) |
| typing-inspection | `0.4.4` | MIT | runtime-transitive | [上游](https://github.com/pydantic/typing-inspection) | [LICENSE](../third_party/licenses/typing-inspection-0.4.4/LICENSE) |
| typing_extensions | `4.16.0` | PSF-2.0 | runtime-transitive | [上游](https://github.com/python/typing_extensions) | [LICENSE](../third_party/licenses/typing-extensions-4.16.0/LICENSE) |
| tzdata | `2026.5` | Apache-2.0 | postgres-integration-driver-transitive | [上游](https://github.com/python/tzdata) | [LICENSE](../third_party/licenses/tzdata-2026.5/LICENSE), [LICENSE_APACHE](../third_party/licenses/tzdata-2026.5/licenses/LICENSE_APACHE) |
| uvicorn | `0.54.0` | BSD-3-Clause | direct-runtime | [上游](https://github.com/Kludex/uvicorn) | [LICENSE.md](../third_party/licenses/uvicorn-0.54.0/LICENSE.md) |

## 来源与校验

- 机器可读记录：[`third_party/licenses/manifest.json`](../third_party/licenses/manifest.json)，包括每份许可文件的 SHA-256。
- 所有本地许可文本逐字复制自实际安装版本的 `.dist-info/licenses` 或等价路径；原有 23 包记录和文本保留，不用另一版本的网上文件替换。
- `packaging` 与 `cryptography` 的双许可文本、Pygments 的 AUTHORS、tzdata 随包的 Apache 文本均一并保留。
- `cffi 2.1.1` 的发行元数据为 MIT-0；`certifi` 为 MPL-2.0，`typing_extensions` 为 PSF-2.0，`psycopg/psycopg-binary` 为 LGPL-3.0-only，没有把全部包概括为 MIT。
- 本清单登记的是实际发行包随附文本与来源。若后续分发含 psycopg-binary 等包及其 bundled native libraries 的镜像/二进制，应按具体发行物保留各自许可、库来源与所需材料；当前源码包不复制安装环境中的原生依赖。

## 本地服务运行时

原生 NATS Server v2.15.1 的官方 ZIP、SHA256SUMS、Apache-2.0 LICENSE 与校验 manifest 保存在 `work/loom-infra/nats`，仅供本地集成测试，不纳入源码包依赖许可证计数。下载核验与单节点恢复边界见 [NATS 集成记录](nats-integration.md)。原生 PostgreSQL 测试运行时同样位于 work，未打包进源码目录。

## 调研与实际接入的区分

首轮 [GitHub 复用记录](github-reuse.md) 保留 2026-10-22 的选型结论：当时 nats.py 尚未接入。2026-10-23 已实际安装 nats-py 并通过相应消息集成测试，更新事实以本清单与 [NATS 集成记录](nats-integration.md) 为准。Temporal 和 LangGraph 仍仅为调研候选，没有安装或打包为本轮应用依赖。

## 更新方式

依赖变化后，读取实际版本元数据，复制随包 LICENSE/NOTICE/AUTHORS 等文件，更新 manifest 的版本、来源、作用范围和摘要，并核对锁文件、对应阶段测试与本表一致。
