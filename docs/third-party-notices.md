# 第三方依赖与许可证声明

记录日期：2026-10-22（Asia/Shanghai）。本清单来自本轮项目隔离环境中已安装发行包的 `METADATA` 和随包许可证文件；仅复制许可与署名文本，没有复制上游实现代码。安装器 pip 不属于应用依赖，未纳入。

应用自己的许可证尚未在此文件选定。分发时应保留相应许可证、版权与署名原文；具体条款以随包文件为准。当前源码仓库没有内嵌这些发行包的实现；将虚拟环境、镜像或二进制一并分发时，应随对应依赖保留这些文件。

## 已安装依赖

FastAPI、Pydantic、Uvicorn 为本轮直接运行依赖；HTTPX 与 pytest 用于 API/内核验证。下表也保留隔离环境中的传递依赖，避免只登记直接依赖而漏掉其他许可证。实际依赖版本以项目锁文件为准。

| 包 | 已安装版本 | 许可证 | 作用范围 | 官方来源 | 本地许可原文 |
|---|---|---|---|---|---|
| annotated-doc | `0.0.5` | MIT | runtime-transitive | [上游](https://github.com/fastapi/annotated-doc) | [LICENSE](../third_party/licenses/annotated-doc-0.0.5/LICENSE) |
| annotated-types | `0.8.0` | MIT | runtime-transitive | [上游](https://github.com/annotated-types/annotated-types) | [LICENSE](../third_party/licenses/annotated-types-0.8.0/LICENSE) |
| anyio | `4.15.1` | MIT | runtime-transitive | [上游](https://github.com/agronholm/anyio) | [LICENSE](../third_party/licenses/anyio-4.15.1/LICENSE) |
| certifi | `2026.10.19` | MPL-2.0 | test-or-test-transitive | [上游](https://github.com/certifi/python-certifi) | [LICENSE](../third_party/licenses/certifi-2026.10.19/LICENSE) |
| click | `8.5.0` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/pallets/click/) | [LICENSE.txt](../third_party/licenses/click-8.5.0/LICENSE.txt) |
| colorama | `0.4.6` | BSD-3-Clause (from packaged LICENSE.txt) | runtime-transitive | [上游](https://github.com/tartley/colorama) | [LICENSE.txt](../third_party/licenses/colorama-0.4.6/LICENSE.txt) |
| fastapi | `0.159.1` | MIT | direct-runtime | [上游](https://github.com/fastapi/fastapi) | [LICENSE](../third_party/licenses/fastapi-0.159.1/LICENSE) |
| h11 | `0.16.0` | MIT | runtime-transitive | [上游](https://github.com/python-hyper/h11) | [LICENSE.txt](../third_party/licenses/h11-0.16.0/LICENSE.txt) |
| httpcore | `1.0.9` | BSD-3-Clause | test-or-test-transitive | [上游](https://github.com/encode/httpcore) | [LICENSE.md](../third_party/licenses/httpcore-1.0.9/LICENSE.md) |
| httpx | `0.28.1` | BSD-3-Clause | test-or-test-transitive | [上游](https://github.com/encode/httpx) | [LICENSE.md](../third_party/licenses/httpx-0.28.1/LICENSE.md) |
| idna | `3.20` | BSD-3-Clause | test-or-test-transitive | [上游](https://github.com/kjd/idna) | [LICENSE.md](../third_party/licenses/idna-3.20/LICENSE.md) |
| iniconfig | `2.3.1` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/iniconfig) | [LICENSE](../third_party/licenses/iniconfig-2.3.1/LICENSE) |
| opentelemetry-api | `1.45.1` | Apache-2.0 | runtime-transitive | [上游](https://github.com/open-telemetry/opentelemetry-python) | [LICENSE](../third_party/licenses/opentelemetry-api-1.45.1/LICENSE) |
| packaging | `26.3` | Apache-2.0 OR BSD-2-Clause | test-or-test-transitive | [上游](https://github.com/pypa/packaging) | [LICENSE](../third_party/licenses/packaging-26.3/LICENSE), [LICENSE.APACHE](../third_party/licenses/packaging-26.3/LICENSE.APACHE), [LICENSE.BSD](../third_party/licenses/packaging-26.3/LICENSE.BSD) |
| pluggy | `1.7.0` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/pluggy) | [LICENSE](../third_party/licenses/pluggy-1.7.0/LICENSE) |
| pydantic | `2.14.1` | MIT | direct-runtime | [上游](https://github.com/pydantic/pydantic) | [LICENSE](../third_party/licenses/pydantic-2.14.1/LICENSE) |
| pydantic_core | `2.50.1` | MIT | runtime-transitive | [上游](https://github.com/pydantic/pydantic/tree/main/pydantic-core) | [LICENSE](../third_party/licenses/pydantic-core-2.50.1/LICENSE) |
| Pygments | `2.21.0` | BSD-2-Clause | test-or-test-transitive | [上游](https://github.com/pygments/pygments) | [AUTHORS](../third_party/licenses/pygments-2.21.0/AUTHORS), [LICENSE](../third_party/licenses/pygments-2.21.0/LICENSE) |
| pytest | `9.1.1` | MIT | test-or-test-transitive | [上游](https://github.com/pytest-dev/pytest) | [LICENSE](../third_party/licenses/pytest-9.1.1/LICENSE) |
| starlette | `1.9.0` | BSD-3-Clause | runtime-transitive | [上游](https://github.com/Kludex/starlette) | [LICENSE.md](../third_party/licenses/starlette-1.9.0/LICENSE.md) |
| typing-inspection | `0.4.4` | MIT | runtime-transitive | [上游](https://github.com/pydantic/typing-inspection) | [LICENSE](../third_party/licenses/typing-inspection-0.4.4/LICENSE) |
| typing_extensions | `4.16.0` | PSF-2.0 | runtime-transitive | [上游](https://github.com/python/typing_extensions) | [LICENSE](../third_party/licenses/typing-extensions-4.16.0/LICENSE) |
| uvicorn | `0.54.0` | BSD-3-Clause | direct-runtime | [上游](https://github.com/Kludex/uvicorn) | [LICENSE.md](../third_party/licenses/uvicorn-0.54.0/LICENSE.md) |

## 来源与校验

- 机器可读记录：[`third_party/licenses/manifest.json`](../third_party/licenses/manifest.json)，包括每份许可文件的 SHA-256。
- 所有本地许可文本均逐字复制自已安装包的 `.dist-info/licenses` 或等价路径；不将网上其他版本的许可证替换成本次发行包文本。
- `packaging` 的 Apache/BSD 两套许可、Pygments 的 AUTHORS 文本均一并保留。
- `certifi` 使用 MPL-2.0；本轮未修改其包内文件。若后续修改或重新打包发行，应按具体发行物重新核对对应文件与来源记录。
- `typing_extensions` 使用 PSF-2.0；`opentelemetry-api` 使用 Apache-2.0；没有把全部传递依赖概括为 MIT。

## 调研但未接入的项目

Temporal、LangGraph、nats.py 的选型与暂缓理由见 [GitHub 复用记录](github-reuse.md)。这些调研项目未安装为本轮应用依赖，因此没有在本目录伪造其随包许可副本。后续接入时应按实际锁定发行物更新本清单。

## 更新方式

依赖版本变化后，重新读取该版本的安装元数据，复制随包 LICENSE/NOTICE/AUTHORS 等许可署名文件，更新 manifest 的版本和摘要，并核对锁文件、测试与本表一致。
