# VideoCaptioner 文档

这里存放 VideoCaptioner fork 的文档站源码，使用 [VitePress](https://vitepress.dev/) 构建。

## 在线地址

[https://jsonborn98.github.io/VideoCaptioner/](https://jsonborn98.github.io/VideoCaptioner/)

## 本地运行

以下命令均在 `docs/` 目录执行：

```bash
bun install
bun run docs:dev
```

开发服务器默认可从 `http://localhost:5173/VideoCaptioner/` 访问。

构建和预览静态站点：

```bash
bun run docs:build
bun run docs:preview
```

构建产物位于 `.vitepress/dist/`。

## 目录结构

```text
docs/
├── .vitepress/    # 站点配置与自定义主题
├── public/        # 图片等静态资源
├── guide/         # 中文使用说明
├── config/        # 功能与服务配置说明
├── dev/           # 开发记录和归档材料
├── en/            # 英文文档
├── cli.md         # CLI 使用说明
└── index.md       # 中文首页
```

## 自动部署

`docs/` 或部署工作流的变更推送到 `master`、`main` 或 `dev` 后，GitHub Actions 会自动构建文档；其中 `master` 和 `main` 的构建结果会部署到 GitHub Pages。也可以在 Actions 中手动触发部署工作流。
