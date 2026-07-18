# RFID 手环与边缘设备模拟器

这是一个独立于三个业务代码仓库的测试客户端。它直接使用后端现有的 TCP/HTTP 协议，模拟 UHF/HF 读写器、相机、灯光等控制设备和一只四标签手环。仅依赖 Python 3.10+ 标准库，无需安装第三方包。

## 可视化界面（推荐）

1. 启动后端及其 PostgreSQL、Redis、MinIO 依赖。
2. 复制成都多景点配置，按本机环境修改地址和用户 ID。
3. 设置管理员密码，初始化 8 个景点及其设备，然后启动界面：

```powershell
Copy-Item config.web.example.json config.json
$env:SIM_ADMIN_PASSWORD = "你的管理员密码"
.\run.ps1 -Command provision
.\run.ps1
```

浏览器会打开 `http://127.0.0.1:8765/`。先点“连接全部设备”，再点击任一景点卡片：第一次表示进入，第二次表示离开；点击另一个景点会先自动离开当前景点。

界面包含武侯祠、成都大熊猫繁育研究基地、杜甫草堂、金沙遗址博物馆、宽窄巷子、青城山、都江堰景区和春熙路·太古里。每个景点使用不同的 UHF-A/B/C、HF、相机和灯光组合。

也可以一条命令先初始化再启动界面：

```powershell
python -m edge_simulator --config config.json web --provision
```

## 命令行场景

如果需要原来的单点命令行场景，复制 `config.example.json` 为 `config.json` 后执行 `full`。它会先调用 Admin API 初始化测试数据，再执行以下链路：

1. 用两条带历史时间的 UHF-A 事件模拟 35 秒停留，无需真的等待。
2. 上报 UHF-B，触发相机命令、ACK 和模拟 JPEG 上传。
3. 上报 UHF-C，触发灯光命令和 ACK。
4. 上报 HF，生成当前用户的现场控制权限。

如果测试数据已经初始化，可直接运行：

```powershell
.\run.ps1 -Command scenario
.\run.ps1 -Command interactive
```

## 常用命令

```powershell
python -m edge_simulator --config config.json validate
python -m edge_simulator --config config.json provision
python -m edge_simulator --config config.json doctor --admin-audit
python -m edge_simulator --config config.json --verbose scenario
python -m edge_simulator --config config.json web --no-browser
```

交互控制台支持掉线/重连、切换 ACK 结果、单独触发四种标签、手动上传图片，以及通过 App API 下发 HF 现场控制命令。进入后输入 `help` 查看完整命令。

App API 需要 JWT。可将小程序登录得到的 token 放入 `SIM_APP_TOKEN`；也可把后端 JWT 密钥放入 `SIM_JWT_SECRET`，模拟器会用 `app.user_id` 在本地签发测试 token。不要把密码、JWT 或密钥写进 `config.json` 或提交到版本库。

完整设计、配置和验收步骤见 `docs/docs/files/边缘设备与手环交互模拟器说明.md`。
