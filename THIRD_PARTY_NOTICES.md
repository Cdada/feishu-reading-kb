# 第三方依赖说明

项目原创代码适用根目录 MIT 许可。依赖的许可证由各自作者提供；MIT 不替代这些许可，也不覆盖用户输入的资料、模型权重、字体和外部服务条款。

本仓库分发源码、锁定清单与合成测试，不附带依赖源码、运行器、字体、MinerU 或模型权重。安装依赖时应保留安装包内的 LICENSE / NOTICE。若另行分发容器、二进制或打包应用，需要根据实际包含的依赖重新收集所需声明。

## 核心依赖

| 软件 | 锁定版本 | 上游许可 |
| --- | --- | --- |
| markdown-it-py | 3.0.0 | MIT |
| mdurl | 0.1.2 | MIT |
| Pillow | 12.3.0 | MIT-CMU；所带图像库另见其包内声明 |
| ReportLab | 4.4.9 | BSD，详见包内 license.txt |
| pypdf | 6.10.0 | BSD-3-Clause |
| charset-normalizer | 3.4.4 | MIT |
| PyYAML | 6.0.3 | MIT |
| @larksuiteoapi/node-sdk | 1.73.3 | MIT |
| github.com/larksuite/cli | v1.0.97 | MIT |
| github.com/larksuite/oapi-sdk-go/v3 | v3.7.2 | MIT |
| github.com/gorilla/websocket | v1.5.3 | BSD-2-Clause |

Python 精确版本以 `projects/kb-ingestion-workflow/integrations/kb_custody_v21/requirements.lock` 为准。可选 MCP 转换接口使用自己的 `pyproject.toml` 和 `uv.lock`，不属于核心离线回归的安装集合；其 MCP SDK 及传递依赖应按该锁定文件安装并保留包内许可。

Node 依赖许可标识来自所提交的 `package-lock.json`。Go 模块清单来自 `go.mod`，链接指向对应版本的许可页；这份清单不是对所有传递依赖的法律结论。

## Node 锁定依赖

| 包 | 版本 | 许可标识 |
| --- | --- | --- |
| `@larksuiteoapi/node-sdk` | 1.73.3 | MIT |
| `@protobufjs/aspromise` | 1.1.2 | BSD-3-Clause |
| `@protobufjs/base64` | 1.1.2 | BSD-3-Clause |
| `@protobufjs/codegen` | 2.0.5 | BSD-3-Clause |
| `@protobufjs/eventemitter` | 1.1.1 | BSD-3-Clause |
| `@protobufjs/fetch` | 1.1.1 | BSD-3-Clause |
| `@protobufjs/float` | 1.0.2 | BSD-3-Clause |
| `@protobufjs/path` | 1.1.2 | BSD-3-Clause |
| `@protobufjs/pool` | 1.1.0 | BSD-3-Clause |
| `@protobufjs/utf8` | 1.1.2 | BSD-3-Clause |
| `@types/node` | 26.4.1 | MIT |
| `agent-base` | 6.0.2 | MIT |
| `asynckit` | 0.4.0 | MIT |
| `axios` | 1.20.0 | MIT |
| `call-bind-apply-helpers` | 1.0.2 | MIT |
| `call-bound` | 1.0.4 | MIT |
| `combined-stream` | 1.0.8 | MIT |
| `debug` | 4.4.3 | MIT |
| `delayed-stream` | 1.0.0 | MIT |
| `dunder-proto` | 1.0.1 | MIT |
| `es-define-property` | 1.0.1 | MIT |
| `es-errors` | 1.3.0 | MIT |
| `es-object-atoms` | 1.1.2 | MIT |
| `es-set-tostringtag` | 2.1.0 | MIT |
| `follow-redirects` | 1.16.0 | MIT |
| `form-data` | 4.0.6 | MIT |
| `function-bind` | 1.1.2 | MIT |
| `get-intrinsic` | 1.3.0 | MIT |
| `get-proto` | 1.0.1 | MIT |
| `gopd` | 1.2.0 | MIT |
| `has-symbols` | 1.1.0 | MIT |
| `has-tostringtag` | 1.0.2 | MIT |
| `hasown` | 2.0.4 | MIT |
| `https-proxy-agent` | 5.0.1 | MIT |
| `lodash.identity` | 3.0.0 | MIT |
| `lodash.merge` | 4.6.2 | MIT |
| `lodash.pickby` | 4.6.0 | MIT |
| `long` | 5.3.2 | Apache-2.0 |
| `math-intrinsics` | 1.1.0 | MIT |
| `mime-db` | 1.52.0 | MIT |
| `mime-types` | 2.1.35 | MIT |
| `ms` | 2.1.3 | MIT |
| `object-inspect` | 1.13.4 | MIT |
| `protobufjs` | 7.6.6 | BSD-3-Clause |
| `proxy-from-env` | 2.1.0 | MIT |
| `qs` | 6.16.0 | BSD-3-Clause |
| `side-channel` | 1.1.1 | MIT |
| `side-channel-list` | 1.0.1 | MIT |
| `side-channel-map` | 1.0.1 | MIT |
| `side-channel-weakmap` | 1.0.2 | MIT |
| `undici-types` | 8.3.0 | MIT |
| `ws` | 8.21.3 | MIT |

## Go 锁定模块

| 模块 | 版本 | 许可来源 |
| --- | --- | --- |
| `github.com/Microsoft/go-winio` | `v0.6.2` | [上游模块许可](https://pkg.go.dev/github.com/Microsoft/go-winio@v0.6.2?tab=licenses) |
| `github.com/atotto/clipboard` | `v0.1.4` | [上游模块许可](https://pkg.go.dev/github.com/atotto/clipboard@v0.1.4?tab=licenses) |
| `github.com/aymanbagabas/go-osc52/v2` | `v2.0.1` | [上游模块许可](https://pkg.go.dev/github.com/aymanbagabas/go-osc52/v2@v2.0.1?tab=licenses) |
| `github.com/bmatcuk/doublestar/v4` | `v4.10.0` | [上游模块许可](https://pkg.go.dev/github.com/bmatcuk/doublestar/v4@v4.10.0?tab=licenses) |
| `github.com/catppuccin/go` | `v0.3.0` | [上游模块许可](https://pkg.go.dev/github.com/catppuccin/go@v0.3.0?tab=licenses) |
| `github.com/charmbracelet/bubbles` | `v0.21.1-0.20250623103423-23b8fd6302d7` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/bubbles@v0.21.1-0.20250623103423-23b8fd6302d7?tab=licenses) |
| `github.com/charmbracelet/bubbletea` | `v1.3.6` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/bubbletea@v1.3.6?tab=licenses) |
| `github.com/charmbracelet/colorprofile` | `v0.2.3-0.20250311203215-f60798e515dc` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/colorprofile@v0.2.3-0.20250311203215-f60798e515dc?tab=licenses) |
| `github.com/charmbracelet/huh` | `v1.0.0` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/huh@v1.0.0?tab=licenses) |
| `github.com/charmbracelet/lipgloss` | `v1.1.0` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/lipgloss@v1.1.0?tab=licenses) |
| `github.com/charmbracelet/x/ansi` | `v0.9.3` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/x/ansi@v0.9.3?tab=licenses) |
| `github.com/charmbracelet/x/cellbuf` | `v0.0.13` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/x/cellbuf@v0.0.13?tab=licenses) |
| `github.com/charmbracelet/x/exp/strings` | `v0.0.0-20240722160745-212f7b056ed0` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/x/exp/strings@v0.0.0-20240722160745-212f7b056ed0?tab=licenses) |
| `github.com/charmbracelet/x/term` | `v0.2.1` | [上游模块许可](https://pkg.go.dev/github.com/charmbracelet/x/term@v0.2.1?tab=licenses) |
| `github.com/danieljoos/wincred` | `v1.2.3` | [上游模块许可](https://pkg.go.dev/github.com/danieljoos/wincred@v1.2.3?tab=licenses) |
| `github.com/dustin/go-humanize` | `v1.0.1` | [上游模块许可](https://pkg.go.dev/github.com/dustin/go-humanize@v1.0.1?tab=licenses) |
| `github.com/erikgeiser/coninput` | `v0.0.0-20211004153227-1c3628e74d0f` | [上游模块许可](https://pkg.go.dev/github.com/erikgeiser/coninput@v0.0.0-20211004153227-1c3628e74d0f?tab=licenses) |
| `github.com/godbus/dbus/v5` | `v5.2.2` | [上游模块许可](https://pkg.go.dev/github.com/godbus/dbus/v5@v5.2.2?tab=licenses) |
| `github.com/gofrs/flock` | `v0.8.1` | [上游模块许可](https://pkg.go.dev/github.com/gofrs/flock@v0.8.1?tab=licenses) |
| `github.com/gogo/protobuf` | `v1.3.2` | [上游模块许可](https://pkg.go.dev/github.com/gogo/protobuf@v1.3.2?tab=licenses) |
| `github.com/google/uuid` | `v1.6.0` | [上游模块许可](https://pkg.go.dev/github.com/google/uuid@v1.6.0?tab=licenses) |
| `github.com/gorilla/websocket` | `v1.5.3` | [上游模块许可](https://pkg.go.dev/github.com/gorilla/websocket@v1.5.3?tab=licenses) |
| `github.com/inconshreveable/mousetrap` | `v1.1.0` | [上游模块许可](https://pkg.go.dev/github.com/inconshreveable/mousetrap@v1.1.0?tab=licenses) |
| `github.com/itchyny/gojq` | `v0.12.17` | [上游模块许可](https://pkg.go.dev/github.com/itchyny/gojq@v0.12.17?tab=licenses) |
| `github.com/itchyny/timefmt-go` | `v0.1.6` | [上游模块许可](https://pkg.go.dev/github.com/itchyny/timefmt-go@v0.1.6?tab=licenses) |
| `github.com/larksuite/oapi-sdk-go/v3` | `v3.7.2` | [上游模块许可](https://pkg.go.dev/github.com/larksuite/oapi-sdk-go/v3@v3.7.2?tab=licenses) |
| `github.com/lucasb-eyer/go-colorful` | `v1.2.0` | [上游模块许可](https://pkg.go.dev/github.com/lucasb-eyer/go-colorful@v1.2.0?tab=licenses) |
| `github.com/mattn/go-isatty` | `v0.0.20` | [上游模块许可](https://pkg.go.dev/github.com/mattn/go-isatty@v0.0.20?tab=licenses) |
| `github.com/mattn/go-localereader` | `v0.0.1` | [上游模块许可](https://pkg.go.dev/github.com/mattn/go-localereader@v0.0.1?tab=licenses) |
| `github.com/mattn/go-runewidth` | `v0.0.16` | [上游模块许可](https://pkg.go.dev/github.com/mattn/go-runewidth@v0.0.16?tab=licenses) |
| `github.com/mitchellh/hashstructure/v2` | `v2.0.2` | [上游模块许可](https://pkg.go.dev/github.com/mitchellh/hashstructure/v2@v2.0.2?tab=licenses) |
| `github.com/muesli/ansi` | `v0.0.0-20230316100256-276c6243b2f6` | [上游模块许可](https://pkg.go.dev/github.com/muesli/ansi@v0.0.0-20230316100256-276c6243b2f6?tab=licenses) |
| `github.com/muesli/cancelreader` | `v0.2.2` | [上游模块许可](https://pkg.go.dev/github.com/muesli/cancelreader@v0.2.2?tab=licenses) |
| `github.com/muesli/termenv` | `v0.16.0` | [上游模块许可](https://pkg.go.dev/github.com/muesli/termenv@v0.16.0?tab=licenses) |
| `github.com/rivo/uniseg` | `v0.4.7` | [上游模块许可](https://pkg.go.dev/github.com/rivo/uniseg@v0.4.7?tab=licenses) |
| `github.com/sergi/go-diff` | `v1.4.0` | [上游模块许可](https://pkg.go.dev/github.com/sergi/go-diff@v1.4.0?tab=licenses) |
| `github.com/skip2/go-qrcode` | `v0.0.0-20200617195104-da1b6568686e` | [上游模块许可](https://pkg.go.dev/github.com/skip2/go-qrcode@v0.0.0-20200617195104-da1b6568686e?tab=licenses) |
| `github.com/spf13/cobra` | `v1.10.2` | [上游模块许可](https://pkg.go.dev/github.com/spf13/cobra@v1.10.2?tab=licenses) |
| `github.com/spf13/pflag` | `v1.0.9` | [上游模块许可](https://pkg.go.dev/github.com/spf13/pflag@v1.0.9?tab=licenses) |
| `github.com/xo/terminfo` | `v0.0.0-20220910002029-abceb7e1c41e` | [上游模块许可](https://pkg.go.dev/github.com/xo/terminfo@v0.0.0-20220910002029-abceb7e1c41e?tab=licenses) |
| `github.com/zalando/go-keyring` | `v0.2.8` | [上游模块许可](https://pkg.go.dev/github.com/zalando/go-keyring@v0.2.8?tab=licenses) |
| `golang.org/x/net` | `v0.43.0` | [上游模块许可](https://pkg.go.dev/golang.org/x/net@v0.43.0?tab=licenses) |
| `golang.org/x/sync` | `v0.16.0` | [上游模块许可](https://pkg.go.dev/golang.org/x/sync@v0.16.0?tab=licenses) |
| `golang.org/x/sys` | `v0.35.0` | [上游模块许可](https://pkg.go.dev/golang.org/x/sys@v0.35.0?tab=licenses) |
| `golang.org/x/term` | `v0.34.0` | [上游模块许可](https://pkg.go.dev/golang.org/x/term@v0.34.0?tab=licenses) |
| `golang.org/x/text` | `v0.28.0` | [上游模块许可](https://pkg.go.dev/golang.org/x/text@v0.28.0?tab=licenses) |
| `gopkg.in/yaml.v3` | `v3.0.1` | [上游模块许可](https://pkg.go.dev/gopkg.in/yaml.v3@v3.0.1?tab=licenses) |

## 工具与可选服务

GitHub Actions 安装 Python、Node、Go 及系统中文字体，它们不被重新收录到仓库。真实飞书、Codex 客户端、MinerU、Dify、百度网盘等能力需要使用者自行取得并配置；项目许可不授予这些服务的账号、商标或收费权益。
