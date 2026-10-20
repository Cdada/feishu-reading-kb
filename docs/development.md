# 离线开发与测试

## 创建隔离工作区

需要 Git、Python 3.12、Node.js 24。完整回归另需 Go 1.27.1 和可用的中文字体。不要使用符号链接路径。

```sh
git clone https://github.com/Cdada/feishu-reading-kb.git
cd feishu-reading-kb
git worktree add -b codex/kb-ingestion-v2.1 ../feishu-reading-kb-dev main
cd ../feishu-reading-kb-dev
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r projects/kb-ingestion-workflow/integrations/kb_custody_v21/requirements.lock
```

当前运行保护要求 `codex/kb-ingestion-v2.1` 分支和 linked worktree（链接工作区）；直接在主克隆目录运行会被拒绝。`dev.example.json` 默认使用模拟发送，不监听、不连接真实飞书，也不启用百度同步。

## 最小演示

```sh
python projects/kb-ingestion-workflow/integrations/kb_custody_v21/dev.py init
python projects/kb-ingestion-workflow/integrations/kb_custody_v21/dev.py demo
python projects/kb-ingestion-workflow/integrations/kb_custody_v21/dev.py status
```

演示只登记一个合成附件，检查原件保管与模拟通知，不生成真实 AI 导读。输出写入被 Git 忽略的 `.dev-runtime/kb-v21/`。

## 验证

轻量的公开内容检查：

```sh
python -m unittest discover -s scripts/tests -v
python scripts/check_public_source.py
python scripts/check_public_source.py --history
git diff --check
```

先 `git add` 明确列出的代码文件，再检查暂存内容；扫描不读取被忽略的私人运行目录。

完整回归安装 Go 锁定依赖和中文字体后运行：

```sh
go -C projects/kb-ingestion-workflow/integrations/kb_custody_v21/f05_cli_guard mod download
python projects/kb-ingestion-workflow/integrations/kb_custody_v21/run_review_tests.py
```

Ubuntu 字体包为 `fonts-wqy-microhei`，可设置 `KB_V21_PREVIEW_FONT=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc`；其他系统提供本机合法取得的中文字体路径。字体不随仓库分发。

GitHub Actions 执行同一套合成回归。安装依赖阶段需要联网，测试阶段带网络限制，不需要飞书凭据或模型密钥。工作流在 `main` 推送、拉取请求和手动触发时运行，同一分支的新运行会取消旧运行。

本地记录在 `.dev-runtime/kb-v21/test-output/reviews/`，包含命令结果与源码一致性检查。未通过的检查应保留失败记录，不能用此前成功替代本次结果。
