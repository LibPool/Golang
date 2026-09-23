# Go 库索引

本目录收录来自 Go 官方模块服务的 Go 库索引，按 Go 大版本与模块路径组织：

- 大版本目录：`go-v1`（Go 语言目前为 1.x 大版本）
- 模块路径：`github.com/gin-gonic/gin` 位于 `go-v1/github.com/gin-gonic/gin/gin.md`
- 含大写字母的模块路径按 Go module proxy 约定转义（如 `gin-Gonic` 写作 `gin-!gonic`），保证大小写不同的模块可共存
- 收录来源：`index.golang.org` 模块索引 + `proxy.golang.org` 模块代理
- 当前共收录 43722 个 Go 模块。

## 数据源

- Go module index：https://index.golang.org/index
- Go module proxy：https://proxy.golang.org/
- pkg.go.dev：https://pkg.go.dev/

## 生成方式

```bash
python tools/generate_index.py --crawl --crawl-count 30000
```

按大版本统计：

- go-v1：43722 个模块

数据来自 Go 官方模块索引与代理，可通过上述命令重新生成。
