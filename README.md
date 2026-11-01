# ILT Knowledge Base

面向计算光刻 / ILT 的文件式知识编译器。LangChain 调用 Gemini API（默认）或本地 Ollama，LangGraph 编排扫描 → 提取 → 跨文档综合 → 校验 → 候选版本，以及独立的研究提案和复核流程。候选结论经人工审核后，由管理员明确发布。知识内容以 Markdown/JSON 发布，不使用 embedding 或向量数据库；可选本地网页服务使用 SQLite 保存用户、审核与审计记录。使用 Gemini 无需本地 GPU；构建、问答和研究模型调用会将所需文本及证据发送到 Google，文件与快照保存在运行程序的机器上。

**当前版本为文本原型，已用一份 30 讲计算光刻课程压缩包和一部计算光刻书籍的 12 个 PDF 抽样页测试，尚未经过领域专家验收。** 自动语义复核不能证明科学结论正确；PDF 图像、表格版式、扫描内容及公式保真度明确标记待处理。课程标题提及 ASML，但压缩包本身不能证明由 ASML 出版或背书。

## 安装和运行

Python 3.11+。基础依赖是 `langchain-core` 和 `langgraph`；只安装实际使用的提供商适配。Gemini 另需 `langchain-google-genai`，Ollama 另需 `langchain-ollama`；它们有传递依赖。PDF 另需 `PyMuPDF`。运行时不需要 LangSmith 服务，代码显式关闭构建和模型调用的 tracing。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[gemini,pdf]'
.venv/bin/python -m iltkb init ./my-ilt-kb
```

编辑 `my-ilt-kb/iltkb.json`：

```json
{
  "provider": "gemini",
  "model": "填写你的 Gemini API 账户可用的模型 ID",
  "model_revision": "填写明确模型版本或此次采用的修订标记",
  "api_key_env": "GEMINI_API_KEY",
  "publication_policy": "review_required",
  "max_output_tokens": 8192,
  "chunk_chars": 9000,
  "max_input_chars": 40000,
  "workers": 1
}
```

保留初始化生成的 `topics` 字段。默认主题：成像与工艺、目标与优化、制造约束、评估、待细分主题。允许修改主题、关键词、别名；主题变化使提取缓存失效以重新检查归类。模型更换或版本更新时更新 `model_revision`；云服务可变别名不能保证服务端权重不变，此标记用于显式作废旧缓存。

在运行程序的终端设置 `GEMINI_API_KEY` 环境变量。密钥不写入 `iltkb.json`、源码、提示词或构建指纹，也不要发送到聊天中。例如 Bash 下可交互输入，避免把密钥直接写进命令历史：

```bash
read -rsp 'Gemini API key: ' GEMINI_API_KEY
export GEMINI_API_KEY
```

`api_key_env` 可以更换为其他环境变量名。Gemini 适配显式使用 Developer API 的 key 认证，不会自动切换到 Vertex AI / 本机 Google 身份认证。提供商错误正文不回显，避免把密钥或文档片段写入 CLI 错误信息。

如果密钥已保存在其他目录的 `.env` 中，可在 `iltkb.json` 指定文件路径，不用复制密钥：

```json
{
  "env_file": "/Users/你的用户名/某个目录/.env",
  "api_key_env": "GEMINI_API_KEY"
}
```

这两个字段合并到已有配置中。若文件内名称是 `GOOGLE_API_KEY`，修改 `api_key_env` 即可。相对路径以知识库根目录为基准，支持 `~`。文件必须位于实际运行程序的机器上；云端 Linux 不会直接访问 Mac 的 `/Users/...`。

读取优先级：非空进程环境变量 → 指定 `.env` 内的同名值。只读取选定变量，不把文件内容导出到进程环境，也不执行 shell 命令或变量展开。支持单行 `KEY=value`、单双引号、`export` 和注释；目标值使用多行或重复定义时明确报错。没有增加 dotenv 依赖。`.env` 路径及密钥不会进入构建 manifest 或模型指纹。

把 UTF-8 Markdown 或 PDF 放入 `my-ilt-kb/raw/`，配置好 Gemini 模型与密钥后运行：

```bash
.venv/bin/python -m iltkb build ./my-ilt-kb
.venv/bin/python -m iltkb candidate ./my-ilt-kb
# 启动网页审核候选版并由管理员发布，随后执行：
.venv/bin/python -m iltkb check ./my-ilt-kb
.venv/bin/python -m iltkb search ./my-ilt-kb '不同成像条件下的优化结果如何比较？'
.venv/bin/python -m iltkb read ./my-ilt-kb optimization
.venv/bin/python -m iltkb query ./my-ilt-kb '不同成像条件下的优化结果如何比较？'
```

`candidate`、`search`、`read`、`context` 和 `check` 不调用模型。默认 `publication_policy=review_required`；`build` 只生成一个候选版本并阻止同时构建下一版。`candidate` 输出待审结论、原文摘录、沿用批准情况和发布闸门状态。管理员与审核员在网页“Pending candidate”逐条审核，只有管理员可发布或放弃候选。旧配置若缺少该字段，也按审核后发布处理；只有显式设置 `auto` 才使用旧的自动发布行为。`context ROOT QUESTION` 输出附带原始证据的 JSON，适合其他 Agent 使用。查询结果包含版本 ID；Reader 创建时固定一个发布版本。总目录在 `.iltkb/releases/<release>/wiki/index.md`，沿分组目录进入主题页，证据链接指向不可变原文快照。

## Web 可视化

构建生成候选版本后，在项目根目录启动本地网页服务；首次构建尚无发布版也可进入管理界面：

```bash
.venv/bin/iltkb serve ./my-ilt-kb --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。首次访问会要求创建管理员账户（密码至少 12 字符）；此后所有知识 API 与原始文件都需要登录。管理员可在 **System management** 添加审核员和只读用户、调整角色或停用账号。审核员可对每条结论选择批准、需修改或驳回，记录意见，并人工标记冲突、不一致、过时；模型报告的冲突与分歧也会自动标记。审核记录及操作日志保存在 `.iltkb/management.sqlite3`，按发布版本分开；旧版审核显示为过时。只有主题输入哈希和结论指纹均完全相同、旧批准无风险标记且候选版没有明确新决定时，才沿用旧批准；其余结论须重新审核。审核状态会显示在主题结论上。

管理界面可选择 UTF-8 `.md` 或 `.markdown` 文件、预览文本并上传到 `raw/`。上传不会自动构建；管理员确认资料后执行 `build`，在“Pending candidate”核验并批准，再由管理员发布，最后执行 `check`，才能让它进入发布版。已有 Markdown 导入和原文快照流程同样适用。界面另提供主题地图、证据链、PDF 抽样范围、视觉核验缺口和全局检索（按 `/` 快速打开）。手机和桌面屏幕均可使用。每 10 秒检查一次新发布版本与隔离状态并自动刷新。普通浏览、检索、审核和上传不调用 Gemini；**构建、问答及研究提案/复核会消耗模型预算**。多人同时审核同一结论时，后提交者若持有旧版本会收到冲突提示，需重新查看。

### 受控本体与语义关系（第一阶段）

`schema/ontology.json` 定义类、子类、对象属性、子属性、domain/range、互斥类和已命名领域概念。初始化时生成一个小词汇：结论、原文证据、来源、主题、方法、工艺条件、指标和前提；内置结构不能改写，可在其上增加类、受控属性与概念。例如在 `entities` 中增加 `"ILT": {"label": "逆向光刻技术", "class": "Method"}`，审核员随后可提出“某条结论 `usesMethod` ILT”的待审关系。概念的命名与归类本身由维护 schema 的工程师负责；schema 哈希变化会使旧语义快照暂停、旧关系退出当前审核队列，不能静默沿用。

系统把当前发布版的结论、证据、来源和主题映射为有类型节点。结论引用证据、证据出自来源、结论归属主题属于**编译结构**；`dependsOn`、`refines`、`contradicts`、`usesMethod`、`hasCondition` 等属于**人工审核的语义提案**。关系必须指向当前版本的节点，并引用所涉结论的原始证据。审核关系前，其所依赖的结论必须有当前有效批准；读者图谱只纳入这些结论。提案人不能批准自己的关系；管理员还需在 **System management → Ontology and relations** 单独发布语义快照，读者才能在 **Semantic graph** 浏览关系、规则、推导前提与证据。撤回关系或其依赖的结论审核、更新 schema 或知识发布版、隔离来源，都会令读者语义快照暂停，而不会继续展示可能过时的推导。

本阶段仅实现 [RDFS](https://www.w3.org/TR/rdf-schema/) 的子类、子属性、domain 和 range 推导，以及 [OWL 2 RL](https://www.w3.org/TR/owl2-profiles/) 中互斥类的矛盾检测；**不是完整 OWL 推理机**。domain/range 在 RDFS 中用于推导类型；系统另有明确的应用形状检查，用来拒绝端点类型不匹配的提案，这与 [SHACL](https://www.w3.org/TR/shacl/) 所解决的数据校验问题相近。没有实现 `owl:sameAs`、传递属性、基数、否定、属性链或“缺失即假”的推理。规则一致不代表文献或科学结论正确；自动生成的结构关系和推导结果也不能充当独立证据。当前不调用 LLM 自动创造语义关系，先用人工提案与审核验证词汇和工作流。

后续扩展按验证门槛推进：先用真实案例核对词汇、关系方向与反例，再引入有类型的数值/单位属性和独立形状校验；只有属性数据通过回归与领域抽检后，才考虑少量明确声明的可计算规则。新增规则必须保留证据、适用条件、推导轨迹和撤回路径，并在单独候选快照中评估。LLM 可以提出关系与缺失资料请求，但不得自行批准或把假设写进读者图谱。

无需模型调用即可检查草案及当前读者快照：

```bash
.venv/bin/iltkb ontology-check ./my-ilt-kb
.venv/bin/iltkb ontology-graph ./my-ilt-kb
.venv/bin/iltkb ontology-export ./my-ilt-kb > ontology.nt
```

`ontology-export` 仅导出仍然有效的已发布语义快照，格式为 RDF 1.1 N-Triples；每条事实还带有可追溯规则、前提与 `PROV-O` 来源的再化陈述。网页“Semantic graph”也提供下载入口。未发布或已失效的快照无法导出。

管理界面“Ontology and relations”采用领域工程师工作台：待审、失效、已批准和已驳回关系可筛选与检索；检阅页并排展示关系两端、原文摘录、来源和已有审核意见。提出新关系时按属性类型筛选端点，并从所涉结论的原始证据中勾选，不需手填证据 ID。此页面复用既有审核、权限和发布接口，不改变本体规则；当没有已批准关系时，页面不会引导管理员发布空的语义关系快照。

### 研究提案与主动索取资料

**System management → Research and material requests** 会列出当前版本已报告、尚未认领的知识缺口；审核员可选一个缺口或输入新问题，限定主题后生成一份待验证假设及最多三项具体资料/验证入口请求。LangGraph 按“固定版本检索 → LangChain 模型提案 → 机械校验 → 读取另一批证据作独立反证检查 → 再校验 → 保存”运行，优先覆盖不同来源的已考虑证据，并明确报告仍未纳入的证据数。模型只能引用本次提供的原始证据 ID；无证据时必须请求原始资料。反证检查的“未找到反例”不等于假设成立。提案、反证、评估和人工决定都保存在 `.iltkb/research.sqlite3`，绝不直接写入发布知识。缺口来自既有模型综合，仍须人工判断其研究价值。

审核员可把 UTF-8 `.md`、`.markdown` 或 `.txt` 资料提交至独立的 `.iltkb/research_objects/`，按 SHA-256 保留内容且不自动放入 `raw/`。随后可手动运行一次模型复核，得到“证据不足 / 可能支持 / 可能反驳”的暂定评估；模型必须列出本次使用的原始证据 ID 和提交资料哈希。人工可接收为后续研究任务或驳回，接收**不等于科学验证或发布批准**。若需纳入知识库，仍须独立检查来源、通过现有导入与构建流程。一个研究任务绑定创建时的发布版本；版本变化后不能继续提交、复核或决定，须重新规划。来源隔离期间研究接口暂停。

提交上限为每份 2 MB；模型复核要求每份不超过 16 KB 的聚焦文本且一轮最多八份，超限明确失败而不截断。网页上的提案和复核会调用配置的模型，可能持续较久，受 `token_budget` 账本约束。也可从命令行运行：

```bash
.venv/bin/iltkb research-plan ./my-ilt-kb '哪个证据缺口阻止比较两种优化方法？' --topic optimization
.venv/bin/iltkb research-list ./my-ilt-kb
.venv/bin/iltkb research-show ./my-ilt-kb TASK_ID
.venv/bin/iltkb research-critique ./my-ilt-kb TASK_ID
.venv/bin/iltkb research-assess ./my-ilt-kb TASK_ID
```

`research-list`、`research-show`、资料提交与人工决定不调用模型；新问题的 `research-plan` 最多调用两次模型（提案和反证），一次 `research-assess` 调用一次。提案通过机械校验后先持久化为 `awaiting_critique`；若第二次模型调用失败，可用 `research-critique` 或管理界面重试该节点，不重复提案。相同发布版、主题和问题的未驳回任务会直接复用，避免重复调用与重复审核。该流程不会自行运行实验、访问外部 URL、验证文件真伪，也不把文字声称的测试结果当作物理验证。默认 `build` 只生成待审核候选版；机器校验与人工批准也不能证明所有科学结论正确。研究流程保持在发布知识之外。

默认仅监听本机地址。未配置证书时，程序会拒绝监听非本机地址。若供其他机器上的用户访问，应启用下述 HTTPS；使用 HTTPS 反向代理时，让本程序仍监听 `127.0.0.1`。首次管理员设置只能从服务所在机器完成。

### 家庭局域网访问

`serve` 也接受 `--certfile` 和 `--keyfile`，可直接启用 HTTPS 并绑定到 Mac 的局域网 IP。例如当前机器为 `192.168.178.38`：

```bash
mkdir -p -m 700 ./my-ilt-kb/.iltkb/tls
openssl req -x509 -newkey rsa:2048 -noenc -days 30 \
  -subj /CN=192.168.178.38 \
  -addext subjectAltName=IP:192.168.178.38,IP:127.0.0.1 \
  -addext extendedKeyUsage=serverAuth \
  -keyout ./my-ilt-kb/.iltkb/tls/lan-key.pem \
  -out ./my-ilt-kb/.iltkb/tls/lan-cert.pem
chmod 600 ./my-ilt-kb/.iltkb/tls/lan-key.pem
```

这组证书已经为当前机器生成；只需在证书过期或局域网 IP 改变时重新生成。然后启动：

```bash
.venv/bin/iltkb serve ./my-ilt-kb --host 192.168.178.38 --port 8766 \
  --certfile ./my-ilt-kb/.iltkb/tls/lan-cert.pem \
  --keyfile ./my-ilt-kb/.iltkb/tls/lan-key.pem
```

同一家庭网络中的设备访问 `https://192.168.178.38:8766/`。当前证书为本机生成的自签名证书，浏览器会提示不受信任；先在 Mac 上用 `openssl x509 -in ./my-ilt-kb/.iltkb/tls/lan-cert.pem -noout -fingerprint -sha256` 核对指纹，再在设备上信任证书。私钥目录仅本机可读。证书包含当前 IP，有效期 30 天；IP 改变或过期后需要重新生成。Mac 的防火墙或路由器的客户端隔离可能阻止其他设备连接。首次管理员设置先在这台 Mac 上打开 `http://127.0.0.1:8765/` 完成，之后其他设备使用各自账号登录。

本机示例知识库使用 `gemini-3.1-flash-lite`，在 `my-ilt-kb/iltkb.json` 中设置 `token_budget: 3000000`。程序每次请求前计数并预留输入、最大输出及余量，成功时登记返回的用量；失败或无可用用量时按预留量保守计入。账本在 `my-ilt-kb/.iltkb/token_budget.json`。这是本地请求控制，不代表 Google 账户余额或订阅额度；供应商账单以账户后台为准。

仓库中的 `scripts/import_html_course.py` 可把这次 HTML 课程压缩包中的编号章节转为文本 Markdown，保留每个 HTML 条目的 SHA256，并标记 SVG/图片待处理；`scripts/stage_course_chapters.py` 将选定章节及同一来源组元数据放入 `raw/`。原始压缩包保留在 `my-ilt-kb/imports/`。课程页不是独立论文或实验，不应因分成 30 讲就推断有 30 个独立来源。

若以后使用 Ollama，安装 `.[ollama,pdf]` 并通过 `init ROOT --provider ollama` 初始化；默认端点是运行本程序机器上的 `http://localhost:11434`。旧版没有 `provider` 字段的配置仍按 Ollama 解释；改用 Gemini 时添加 `provider: gemini`，设置模型、修订标记和密钥环境变量即可。

## 四项质量约束

- **跨文档综合**：先逐段提取，再让主题综合读取该主题当前全部证据；输出共同支持、分歧、适用条件、知识缺口。两篇来源不自动等于两份独立实验。模型需逐证据报告考虑范围，另一次语义复核检查遗漏与过度概括。
- **证据追溯**：来源稳定 ID 与版本 SHA256 分开；证据记录精确字符位置、章节、行、PDF 页和原文摘录。结论只允许引用原始证据 ID，不能引用生成结论作为独立证据，因此不存在生成结论的循环证据链。语义正确性仍需领域基准评估。
- **增量构建**：已审查的文本段按内容及规则版本缓存；新文件加入旧主题会重建主题。修改、删除、撤回、来源独立性元数据和模型/提示词/分类变更均影响缓存。并行只用于提取，综合与发布统一执行。
- **分层导航**：总目录 → 分组目录 → 主题页 → 原始证据；中文字符 bigram 与英文词召回，支持关键词/别名扩展，页面包含基于来源共现的相关主题链接。相关链接仅是导航，不是新的证据。

ILT 条件包括成像/掩模/光刻胶模型、工艺角、网格与单位、目标函数、求解器、初始化、制造约束、版图/数据集、硬件和计时边界。缺失条件不能用模型常识补齐，也不能把不同条件下的结果直接归为矛盾。

## 来源元数据

可为 `raw/paper.pdf` 添加 `raw/paper.pdf.meta.json`：

```json
{
  "source_id": "paper-stable-id",
  "title": "Paper title",
  "origin_group": "original-experiment-or-primary-source-id",
  "status": "active"
}
```

不填写时自动分配 ID；无歧义的文件重命名可保持身份，重命名且改内容时应使用显式 ID。同一实验、转载或转述的材料应设置相同 `origin_group`；未知来源关系保持未知，不自动声明独立支持。`status: withdrawn` 保留快照但从证据和后续综合中排除，改回 `active` 可恢复。

PDF 可在元数据中增加 `pdf_pages`，指定按原始 PDF 文件从 1 开始计数的抽样页，例如 `[72, 73, 97]`。必须升序且不重复。程序保存完整原始 PDF 快照及其哈希，只把选中页的文本送往模型；证据页码仍是原文件页码。覆盖报告会明确列出抽样页和其余页未处理的事实。这适合先验证大型 PDF 的文本层与引用链，不能表示读完全文。

## 错误来源的隔离与修复

若已发布的来源包含错误信息，管理员可在 **System management → Source corrections** 先预览影响，再立即隔离该来源。命令行也可执行：

```bash
.venv/bin/iltkb impact ./my-ilt-kb SOURCE_ID
.venv/bin/iltkb quarantine ./my-ilt-kb SOURCE_ID --reason "错误内容及发现依据"
.venv/bin/iltkb incidents ./my-ilt-kb
```

隔离记录写入 `.iltkb/incidents.json`，不会调用模型。隔离期间，所有已发布知识的网页读取、检索和问答都会暂停（HTTP 423）；管理界面仍可供管理员和审核员使用。已创建的命令行 Reader 也会在下一次读取时检查隔离状态。影响报告列出该来源的证据、受影响主题的全部结论数量，以及共享主题或声明同源的其他来源；这些其他来源可能含有同一错误，仍需人工排查。

修复时可在原文件旁的 `.meta.json` 中永久设 `"status": "withdrawn"`，或修改原文件中的错误内容。随后运行 `build`、审核候选版、管理员发布并执行 `check`；构建期间隔离保持生效。原文未改时，编译器强制视为撤回；原文哈希变化后会重新提取并综合。管理员在隔离状态下审核候选版本受影响主题的**每条当前结论**，明确发布后才能在界面解除隔离，或运行：

```bash
.venv/bin/iltkb resolve ./my-ilt-kb SOURCE_ID --reason "修复和审核记录"
```

解除隔离会校验新发布版、确认旧来源版本不再提供证据，并检查受影响主题的批准记录与当前结论指纹一致。若原文件未改，还要求原始 `.meta.json` 中保留 `withdrawn`，避免下次构建把错误再次引入。共享主题的其他来源若复制了相同错误，必须分别纠正或隔离；系统不能仅凭引用关系判定语义转载。隔离只停止对外提供，不会物理删除旧发布、快照、缓存或备份；彻底清除仍需另行制定保留与删除策略。

## 发布、失败和边界

候选先写入 `.iltkb/staging/`；证据、完整文本范围、来源哈希、结论依赖、语义复核和生成导航校验通过后，移入不可变 `.iltkb/releases/`，登记 `.iltkb/pending.json`，不切换读者版本。候选指针绑定 manifest SHA-256；发布时重新校验来源、结论身份、主题输入与审核决定。审核未通过或存在自动冲突标记时不能发布；管理员明确发布后才原子切换 `.iltkb/current.json`。失败不替换上一版。放弃候选只清除待发布指针，保留版本与审核记录供审计。单写者锁防止并发提交；进程异常退出留下锁时，确认无构建进程后再手动删除 `.iltkb/writer.lock`。

大主题超过输入预算时明确失败，**不会截断证据后冒充完成**。首版尚未实现超大主题的分组综合，需细分主题或按模型真实能力调整上下文配置。字符预算是保守保护，不等于 tokenizer 计量；输入与输出预算必须与模型能力匹配。Gemini 因 token 上限、过滤等未正常结束的响应不能用于发布。空提取需理由和复核；覆盖记录表示文本被提交处理，不证明模型没有语义遗漏。

PDF 保存二进制原始快照和文本快照，使用文本层，不做 OCR。每一页都列为待视觉核验；扫描 PDF 没有文本时明确报告无文本，不能声称全文已分析。Markdown 图片只登记待处理，当前不读取图片内容。公式精确性需要后续视觉/人工验收。

`.iltkb/objects`、缓存、旧发布和失败候选不会自动删除，以保护追溯和读者持有的旧版本；长期使用的清理策略尚未实现。对部分格式错误会做有界重试或受限机械修正并留下暂存记录；语义复核失败、坏 JSON 或超预算仍停止构建。没有独立第二模型审查。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用明确标记的合成文本和脚本化模型，通过真实 LangGraph 执行；覆盖增量、反证加入、删除/撤回/恢复、重命名、并发写、证据伪造、引用自证、遗漏、复核失败、中文检索、PDF 页定位与视觉缺口、LangChain Ollama 消息适配。模型适配测试替换了底层聊天调用，不会下载或启动模型。

课程全文与书籍 PDF 抽样测试已完成；下一阶段需要领域专家参考答案、原始图示与公式核验，以及更多可证明出处的独立论文或工艺资料。问答的 ID 与定位校验不能保证每句都被所引证据支持，见 [docs/VALIDATION.md](docs/VALIDATION.md)。详细阶段计划见 [docs/PLAN.md](docs/PLAN.md)。
