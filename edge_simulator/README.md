# RFID 手环与边缘设备模拟器

这是一个独立于三个业务代码仓库的测试客户端。它直接使用后端现有的 TCP/HTTP 协议，模拟 UHF/HF 读写器、相机、灯光、音响、喷雾、投影、雾幕、屏幕等控制设备和一只四标签手环。仅依赖 Python 3.10+ 标准库，无需安装第三方包。

## 可视化界面（推荐）

1. 启动后端及其 PostgreSQL、Redis、MinIO 依赖。
2. 使用默认的杜甫草堂八景点配置，按本机环境修改地址和用户 ID。
3. 设置管理员密码，初始化 8 个景点及其设备，然后启动界面：

```powershell
Copy-Item config.web.example.json config.json
$env:SIM_ADMIN_PASSWORD = "你的管理员密码"
.\run.ps1 -Command provision
.\run.ps1
```

浏览器会打开 `http://127.0.0.1:8765/`。先点“连接全部设备”，再点击任一景点卡片：

- 点击“进入景点”只自动上报 UHF-A，并用历史事件时间立即形成有效停留；不会自动触发其他标签。
- 每个景点都可手动触发 UHF-B 与 UHF-C；按钮会通过 App API 读取当前绑定并显示目标设备。景点设备库存固定，绑定到本站未安装的类型时仍会上报真实标签事件，但模拟器不会创建设备。
- HF 分成“景点打卡”和“设备控制”两个独立按钮。每站都有打卡型 Reader；有 HF 可控设备的站点还配置第二个控制型 Reader。
- 测试平台按不同景点去重显示 80% 礼品门槛：八站中完成七站即达标。此进度仅保存在本次测试平台会话，后端现有 `hf_checkin` 事件仍会真实写入。
- 第二次点击表示离开；点击另一个景点会先自动离开当前景点。离开会发送 UHF-A `event_type=leave`，后端立即关闭现场状态并撤销该点位尚未过期的 HF 控制令牌。

测试程序不会热加载 Python 源码。升级或修改 `edge_simulator` 后必须先停止旧的 8765 进程并重新运行 `run.ps1`；只刷新浏览器仍会继续使用旧进程中的协议实现。

界面按导览图配置大廨、诗史堂、柴门、工部祠、少陵草堂碑亭、茅屋故居、水槛·杜甫千诗碑和万佛楼。固定设备清单由 `devices` 决定：例如诗史堂只有相机、柴门只有灯光、茅屋只有相机和喷雾，App 切换绑定不会改变这份清单。`bindings` 中 UHF-B→相机、UHF-C→灯光只用于首次初始化；后续 `provision` 会保留 App 已保存的绑定。

`media/dufu/` 保存相机预埋照片和现场导览图。照片初始不会作为景点封面直接展示；只有固定相机收到成功的 UHF 拍照命令、回传 ACK 并完成上传后，页面才会显示对应拍摄结果。配置文件中的媒体路径相对于配置文件所在目录解析。

`provision` 会根据每个景点的 `tags`，在固定设备 `config_json` 中同步 `interaction_tags`。每个 HF Reader 必须显式填写 `hf_purpose=checkin|control`：`checkin` 只打卡、不发控制令牌；`control` 必须对应唯一一台 `devices[].config.hf_control=true` 的设备。同一景点可以各有一个不同用途的 HF Reader。修改 Reader 用途、景点标签组合或固定设备清单后，需要重新执行一次 `provision`。

控制型 HF 贴卡后，受控设备会处理后端下发的 `hf_control_start`，按 `duration_seconds` 建立本地控制状态，并通过 `/api/edge/hf-control-ack` 回 ACK。收到 `hf_control_end` 或本地计时到期时只清除 HF 控制状态，不改变设备当前物理开关。未处于该状态时收到 `command_type=HF` 的命令会回 `rejected/HF_CONTROL_INACTIVE`。

HF 环境设备按钮通过 App API 下发真实控制命令，因此需要设置 `SIM_APP_TOKEN`，或配置 `app.user_id` 并通过 `SIM_JWT_SECRET` 生成本地测试 JWT。UHF-A/B/C 与 HF 标签上报本身不依赖 App JWT。

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

## 已知限制与后续事项

### 测试平台触发设备后，小程序不会及时显示体验记录

- **复现现象**：在测试平台使用 UHF-B、UHF-C 或 HF 设备控制按钮时，后端能够创建命令，固定设备能够收到命令并回传成功 ACK；相机也能够完成预埋照片上传。但已打开的微信小程序“设备控制”页面不会同步增加对应的体验记录，喷雾、灯光等设备的执行状态也缺少明显反馈。
- **已确认链路**：测试平台触发的标签或控制请求、后端命令分发、设备执行与 ACK 链路均正常；相机拍摄结果也已写入后端图片记录。因此该现象不是模拟设备未执行或命令失败。
- **原因**：小程序当前每两秒刷新一次 `/api/app/control/current`，但页面中的“体验记录”只读取微信本地存储。只有由小程序自身下发、能够立即获得命令 ID 的操作才会写入这份本地记录。测试平台从外部触发的命令不会进入小程序本地存储；后端目前也没有供小程序按用户或景点查询最近命令的列表接口或实时推送通道。相机结果同样没有在该页面轮询图片列表。部分环境设备的 `current_state` 虽会更新，页面目前也未直接呈现其开关或强度状态。
- **影响范围**：仅影响小程序对“外部触发操作”的即时反馈与历史展示，不影响测试平台、后端和模拟设备之间的真实命令执行结果。
- **后续可选方案**：增加按用户/景点查询最近命令与拍照结果的接口，并由小程序轮询合并展示；或采用 WebSocket/SSE 推送命令状态和图片结果，同时补充环境设备 `current_state` 的可视化。该问题无法只通过测试平台可靠解决。
- **当前状态**：已记录，本轮暂不处理，不修改前端、后端或现有交互行为。

完整设计、配置和验收步骤见 `docs/docs/files/边缘设备与手环交互模拟器说明.md`。
